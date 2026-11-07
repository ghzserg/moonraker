# Notifier
#
# Copyright (C) 2022 Pataar <me@pataar.nl>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

from __future__ import annotations

import asyncio
import html
import logging
import mimetypes
import pathlib
import re
import uuid
from urllib.parse import parse_qsl, unquote, urlsplit
from tornado.httpclient import AsyncHTTPClient, HTTPRequest, HTTPResponse
from ..common import JobEvent, RequestType
from ..utils import json_wrapper as jsonw

# Annotation imports
from typing import (
    TYPE_CHECKING,
    Dict,
    Any,
    List,
    Optional,
    Tuple,
    Union,
)

if TYPE_CHECKING:
    from ..confighelper import ConfigHelper
    from ..common import WebRequest
    from .file_manager.file_manager import FileManager
    from .klippy_apis import KlippyAPI as APIComp

BODY_FORMATS = ("text", "html", "markdown")
Data = Union[bytes, bytearray]

class Notifier:
    def __init__(self, config: ConfigHelper) -> None:
        self.server = config.get_server()
        self.notifiers: Dict[str, NotifierInstance] = {}
        self.events: Dict[str, List[NotifierInstance]] = {}
        self.telegram = TelegramTransport(self.server)
        prefix_sections = config.get_prefix_sections("notifier")
        self.register_remote_actions()
        logging.info("Loading notifiers")
        for section in prefix_sections:
            cfg = config[section]
            try:
                notifier = NotifierInstance(cfg, self.telegram)
                for job_event in list(JobEvent):
                    if job_event == JobEvent.STANDBY:
                        continue
                    evt_name = str(job_event)
                    if "*" in notifier.events or evt_name in notifier.events:
                        self.events.setdefault(evt_name, []).append(notifier)
                logging.info(f"Registered notifier: '{notifier.get_name()}'")
            except Exception as e:
                msg = f"Failed to load notifier[{cfg.get_name()}]\n{e}"
                self.server.add_warning(msg, exc_info=e)
                continue
            self.notifiers[notifier.get_name()] = notifier

        self.register_endpoints(config)
        self.server.register_event_handler(
            "job_state:state_changed", self._on_job_state_changed
        )

    def close(self) -> None:
        self.telegram.close()

    def register_remote_actions(self):
        self.server.register_remote_method("notify", self.notify_action)

    async def notify_action(self, name: str, message: str = ""):
        if name not in self.notifiers:
            raise self.server.error(f"Notifier '{name}' not found", 404)
        notifier = self.notifiers[name]
        await notifier.notify("remote_action", [], message)

    async def _on_job_state_changed(
            self,
            job_event: JobEvent,
            prev_stats: Dict[str, Any],
            new_stats: Dict[str, Any]
    ) -> None:
        evt_name = str(job_event)
        for notifier in self.events.get(evt_name, []):
            await notifier.notify(evt_name, [prev_stats, new_stats])

    def register_endpoints(self, config: ConfigHelper):
        self.server.register_endpoint(
            "/server/notifiers/list", RequestType.GET, self._handle_notifier_list
        )
        self.server.register_debug_endpoint(
            "/debug/notifiers/test", RequestType.POST, self._handle_notifier_test
        )

    async def _handle_notifier_list(
        self, web_request: WebRequest
    ) -> Dict[str, Any]:
        return {"notifiers": self._list_notifiers()}

    def _list_notifiers(self) -> List[Dict[str, Any]]:
        return [notifier.as_dict() for notifier in self.notifiers.values()]

    async def _handle_notifier_test(
        self, web_request: WebRequest
    ) -> Dict[str, Any]:

        name = web_request.get_str("name")
        if name not in self.notifiers:
            raise self.server.error(f"Notifier '{name}' not found", 404)
        notifier = self.notifiers[name]

        kapis: APIComp = self.server.lookup_component('klippy_apis')
        result: Dict[str, Any] = await kapis.query_objects(
            {'print_stats': None}, default={})
        print_stats = result.get('print_stats', {})
        print_stats["filename"] = "notifier_test.gcode"  # Mock the filename

        await notifier.notify(notifier.events[0], [print_stats, print_stats])

        return {
            "status": "success",
            "stats": print_stats
        }

class NotifierInstance:
    def __init__(
        self, config: ConfigHelper, telegram: Optional[TelegramTransport] = None
    ) -> None:
        self.config = config
        name_parts = config.get_name().split(maxsplit=1)
        if len(name_parts) != 2:
            raise config.error(f"Invalid Section Name: {config.get_name()}")
        self.server = config.get_server()
        self.name = name_parts[1]
        self.attach = config.gettemplate("attach", None)
        url_template = config.gettemplate("url")
        self.url = url_template.render()

        if re.match(r"\w+?://", self.url) is None:
            raise config.error(f"Invalid url for: {config.get_name()}")

        self.title = config.gettemplate("title", None)
        self.body = config.gettemplate("body", None)
        self.body_format: str = config.get("body_format", 'text').lower()
        if self.body_format not in BODY_FORMATS:
            raise config.error(f"Invalid body_format for {config.get_name()}")
        self.events: List[str] = config.getlist("events", separator=",")
        self.sender: Optional[Union[TelegramSender, AppriseSender]] = None
        try:
            # Native Telegram support, avoids loading Apprise
            self.sender = TelegramSender.from_url(
                self.url, self.body_format, self.name,
                telegram or TelegramTransport(self.server)
            )
        except ValueError as e:
            # Like Apprise, an invalid url does not fail the notifier
            logging.warning(
                f"[notifier {self.name}]: Invalid Telegram url, "
                f"notifications disabled: {e}"
            )
        else:
            if self.sender is None:
                self.sender = AppriseSender(self.url, self.body_format)

    def as_dict(self):
        return {
            "name": self.name,
            "url": self.config.get("url"),
            "title": self.config.get("title", None),
            "body": self.config.get("body", None),
            "body_format": self.config.get("body_format", None),
            "events": self.events,
            "attach": self.attach
        }

    async def notify(
        self, event_name: str, event_args: List, message: str = ""
    ) -> None:
        context = {
            "event_name": event_name,
            "event_args": event_args,
            "event_message": message
        }

        rendered_title = (
            '' if self.title is None else self.title.render(context)
        )
        rendered_body = (
            event_name if self.body is None else self.body.render(context)
        )

        # Verify the attachment
        attachments: List[str] = []
        if self.attach is not None:
            fm: FileManager = self.server.lookup_component("file_manager")
            try:
                rendered = self.attach.render(context)
            except self.server.error as e:
                self.server.add_warning(
                    f"[notifier {self.name}]: The attachment is not valid. The "
                    "template failed to render.",
                    f"notifier {self.name}",
                    exc_info=e
                )
                self.attach = None
            else:
                for item in rendered.splitlines():
                    item = item.strip()
                    if not item:
                        continue
                    if re.match(r"https?://", item) is not None:
                        # Attachment is a url, system check not necessary
                        attachments.append(item)
                        continue
                    attach_path = pathlib.Path(item).expanduser().resolve()
                    if not attach_path.is_file():
                        self.server.add_warning(
                            f"[notifier {self.name}]: Invalid attachment detected, "
                            f"file does not exist: {attach_path}.",
                            f"notifier {self.name}"
                        )
                    elif not fm.can_access_path(attach_path):
                        self.server.add_warning(
                            f"[notifier {self.name}]: Invalid attachment detected, "
                            f"no read permission for the file {attach_path}.",
                            f"notifier {self.name}"
                        )
                    else:
                        attachments.append(str(attach_path))
        if self.sender is not None:
            await self.sender.send(
                rendered_body.strip(), rendered_title.strip(), attachments
            )

    def get_name(self) -> str:
        return self.name


class AppriseSender:
    def __init__(self, url: str, body_format: str) -> None:
        # Apprise is imported only when a notifier needs it, as it adds
        # several megabytes to Moonraker's memory footprint.
        import apprise
        logging.info(f"Using Apprise version {apprise.__version__}")
        self.body_format = body_format
        self.apprise = apprise.Apprise()
        self.apprise.add(url)

    async def send(self, body: str, title: str, attachments: List[str]) -> None:
        await self.apprise.async_notify(
            body, title, body_format=self.body_format,
            attach=None if not attachments else attachments
        )


# Native Telegram support.  The url format follows the Apprise "tgram://"
# plugin so existing urls keep working; urls with options not implemented
# here are passed on to Apprise.

TELEGRAM_MSG_MAXLEN = 4096
TELEGRAM_MAX_SPLIT = 3
TELEGRAM_URL_MAXSIZE = 2 * 1024 * 1024
TELEGRAM_FILE_MAXSIZE = 5 * 1024 * 1024
TELEGRAM_MAX_WAITING = 5
TGRAM_NATIVE_PARAMS = {"silent", "preview", "overflow", "topic", "thread"}
TGRAM_URL_RE = re.compile(
    r"^tgram://(bot)?([a-z0-9_-]+(:[a-z0-9_-]+)?@)?(?P<token_a>[0-9]+)"
    r"(:|%3A)+(?P<remaining>.*)$",
    re.IGNORECASE
)
TGRAM_TOKEN_RE = re.compile(r"^[0-9]+:[a-z0-9_-]+$", re.IGNORECASE)
TGRAM_CHAT_ID_RE = re.compile(
    r"^((?P<idno>-?[0-9]{1,32})|(@|%40)?(?P<name>[a-z_-][a-z0-9_-]+))"
    r"((:|%3A)(?P<topic>[0-9]+))?$",
    re.IGNORECASE
)
TRUE_VALUES = {"1", "y", "yes", "t", "true", "on", "enable", "enabled", "allow"}
# Telegram errors meaning the file can't be sent as photo (vs. e.g. a bad chat)
TGRAM_PHOTO_ERROR_RE = re.compile(r"photo|image|dimension|file", re.IGNORECASE)

def _split_text(text: str, maxlen: int, count: int) -> List[str]:
    # Telegram counts the length in UTF-16 code units
    chunks: List[str] = []
    while text and len(chunks) < count:
        head = text.encode("utf-16-le")[:2 * maxlen].decode("utf-16-le", "ignore")
        cut = max(1, len(head))
        # Prefer to split on a line break
        newline = text.rfind("\n", 0, cut)
        if cut < len(text) and newline > cut // 2:
            cut = newline
        chunk = text[:cut].rstrip()
        if chunk:
            chunks.append(chunk)
        text = text[cut:].lstrip("\n")
    return chunks

def _parse_tgram_url(url: str) -> Tuple[str, List[str], Dict[str, str]]:
    # Returns the first part of the bot token, the path entries and the
    # url parameters.  Raises ValueError, never include the token.
    match = TGRAM_URL_RE.match(url)
    if match is None:
        raise ValueError("unable to parse the bot token")
    path, _, query = match.group("remaining").partition("?")
    entries = [unquote(e) for e in re.split(r"[ \t\r\n,\\/]+", path) if e]
    params = {k.lower(): v for k, v in parse_qsl(query, keep_blank_values=True)}
    return match.group("token_a"), entries, params


class TelegramTransport:
    # Own HTTP client instead of the shared http_client component: that one
    # buffers responses up to 512 MB and has 10 slots shared with the rest of
    # Moonraker.  All Telegram requests are serialized, which bounds memory
    # and keeps the order of the messages.
    api_url = "https://api.telegram.org"

    def __init__(self, server: Any) -> None:
        self.server = server
        self.client: Optional[AsyncHTTPClient] = None
        self.lock = asyncio.Lock()
        self.waiting = 0

    async def fetch(self, url: str, timeout: float, **kwargs) -> HTTPResponse:
        if self.client is None:
            # Responses are small, attachments from urls are limited
            limit = TELEGRAM_URL_MAXSIZE + 65536
            self.client = AsyncHTTPClient(
                force_instance=True, max_clients=2, max_body_size=limit,
                max_buffer_size=limit
            )
        request = HTTPRequest(
            url, connect_timeout=5., request_timeout=timeout, **kwargs
        )
        return await self.client.fetch(request, raise_error=False)

    async def read_file(self, path: pathlib.Path) -> bytes:
        def read() -> bytes:
            if path.stat().st_size > TELEGRAM_FILE_MAXSIZE:
                raise Exception("file too large")
            return path.read_bytes()
        if self.server is not None:
            return await self.server.get_event_loop().run_in_thread(read)
        return await asyncio.get_running_loop().run_in_executor(None, read)

    def close(self) -> None:
        if self.client is not None:
            self.client.close()
            self.client = None


class TelegramSender:
    def __init__(
        self, name: str, token: str, entries: List[str],
        params: Dict[str, str], transport: TelegramTransport
    ) -> None:
        # Raises ValueError on an invalid url, never include the token.
        self.name = name
        self.transport = transport
        self.token = token
        if TGRAM_TOKEN_RE.match(self.token) is None:
            raise ValueError("invalid bot token")
        self.silent = params.get("silent", "").lower() in TRUE_VALUES
        self.preview = params.get("preview", "").lower() in TRUE_VALUES
        self.split = params.get("overflow", "").lower() == "split"
        topic_param = params.get("topic") or params.get("thread") or "0"
        try:
            topic = int(topic_param) or None
        except ValueError:
            raise ValueError(f"invalid topic '{topic_param}'") from None
        self.targets: List[Tuple[Union[int, str], Optional[int]]] = []
        for target in entries:
            tmatch = TGRAM_CHAT_ID_RE.match(target)
            if tmatch is None:
                logging.warning(f"[notifier {name}]: Invalid chat id '{target}'")
                continue
            chat_id: Union[int, str] = int(tmatch.group("idno") or 0)
            if tmatch.group("name") is not None:
                chat_id = f"@{tmatch.group('name')}"
            chat_topic = int(tmatch.group("topic") or 0) or topic
            if (chat_id, chat_topic) not in self.targets:
                self.targets.append((chat_id, chat_topic))
        if not self.targets:
            raise ValueError("no valid chat id specified")

    @classmethod
    def from_url(
        cls, url: str, body_format: str, name: str, transport: TelegramTransport
    ) -> Optional[TelegramSender]:
        # tgram://bot_token/chat_id[:topic][/chat_id2...][?param=value...]
        # Returns None when the url is left to Apprise, raises ValueError
        # on an invalid url.  Only plain text with explicit chat ids and
        # the parameters implemented here is handled natively.
        if url[:8].lower() != "tgram://":
            return None
        token_a, entries, params = _parse_tgram_url(url)
        if (
            body_format != "text" or len(entries) < 2 or
            not set(params) <= TGRAM_NATIVE_PARAMS
        ):
            logging.info(f"[notifier {name}]: Telegram url sent with Apprise")
            return None
        token = f"{token_a}:{entries[0]}"
        return cls(name, token, entries[1:], params, transport)

    async def send(self, body: str, title: str, attachments: List[str]) -> None:
        tr = self.transport
        if tr.waiting >= TELEGRAM_MAX_WAITING:
            logging.warning(
                f"[notifier {self.name}]: Too many pending Telegram "
                "notifications, dropping this one"
            )
            return
        tr.waiting += 1
        try:
            await tr.lock.acquire()
        finally:
            tr.waiting -= 1
        try:
            await self._send(body, title, attachments)
        except Exception:
            logging.exception(
                f"[notifier {self.name}]: Telegram notification failed"
            )
        finally:
            tr.lock.release()

    async def _send(self, body: str, title: str, attachments: List[str]) -> None:
        # Split before escaping, Telegram counts the characters after
        # parsing the html.
        chunks = _split_text(
            f"{title}\n{body}" if title else body, TELEGRAM_MSG_MAXLEN,
            TELEGRAM_MAX_SPLIT if self.split else 1
        )
        messages = [html.escape(c, quote=False) for c in chunks]
        if title and chunks and chunks[0].startswith(title):
            messages[0] = "<b>%s</b>%s" % (
                html.escape(title, quote=False),
                html.escape(chunks[0][len(title):], quote=False)
            )
        logging.info(
            f"[notifier {self.name}]: Sending Telegram notification, "
            f"{len(attachments)} attachment(s)"
        )
        # Attachments are loaded first, a webcam snapshot is then taken
        # before the slower requests to Telegram.
        files = []
        for item in attachments:
            loaded = await self._load_attachment(item)
            if loaded is not None:
                files.append(loaded)
        for chat_id, topic in self.targets:
            base: Dict[str, Any] = {"chat_id": chat_id}
            if topic is not None:
                base["message_thread_id"] = topic
            if self.silent:
                base["disable_notification"] = True
            for msg in messages:
                await self._api(
                    "sendMessage", dict(
                        base, text=msg, parse_mode="HTML",
                        disable_web_page_preview=not self.preview
                    )
                )
            for file in files:
                fname, mime, data, as_photo = file
                if as_photo:
                    code, desc = await self._api(
                        "sendPhoto", base, ("photo", fname, mime, data)
                    )
                    if code != 400 or not TGRAM_PHOTO_ERROR_RE.search(desc):
                        continue
                code, desc = await self._api(
                    "sendDocument", base, ("document", fname, mime, data)
                )
                if as_photo and code == 200:
                    # Not accepted as photo, send it as document to every chat
                    file[3] = False

    async def _load_attachment(self, item: str) -> Optional[List[Any]]:
        data: Data
        try:
            if re.match(r"https?://", item) is not None:
                data, mime = await self._download(item)
                fname = urlsplit(item).path.rstrip("/").rsplit("/", 1)[-1]
            else:
                path = pathlib.Path(item)
                data, mime, fname = await self.transport.read_file(path), "", path.name
            if not data:
                raise Exception("no data")
        except Exception as e:
            masked = re.sub(r"(://)[^/@]*@", r"\1***@", item)
            logging.warning(
                f"[notifier {self.name}]: Failed to load attachment "
                f"'{masked}': {e or type(e).__name__}"
            )
            return None
        mime = mime or mimetypes.guess_type(fname)[0] or "application/octet-stream"
        ext = "" if "octet-stream" in mime else mimetypes.guess_extension(mime) or ""
        if mime.startswith("image/"):
            # The name means nothing for a photo, but Telegram rejects odd ones
            fname = "snapshot" + (ext or ".jpg")
        else:
            # Keep the name, only add an extension when there is none
            stem, dot, fext = fname.rpartition(".")
            if not dot:
                stem, fext = fname, ext.lstrip(".")
            stem = re.sub(r"[^A-Za-z0-9_-]", "_", stem)[:64] or "attachment"
            fext = re.sub(r"[^A-Za-z0-9]", "", fext)[:8]
            fname = f"{stem}.{fext}" if fext else stem
        return [fname, mime, data, mime.startswith("image/")]

    async def _download(self, url: str) -> Tuple[bytearray, str]:
        # The body is collected here so no second copy is made.  Errors are
        # only recorded, raising in tornado's callbacks logs tracebacks; the
        # client's size limits end oversized or endless downloads (e.g. an
        # mjpeg stream instead of a snapshot), a body delimited by the
        # connection close is truncated by them without an error.
        buf = bytearray()
        info = {"mime": "", "error": ""}

        def on_header(line: str) -> None:
            name, _, val = line.partition(":")
            name, val = name.strip().lower(), val.strip()
            if name == "content-type":
                info["mime"] = val.split(";")[0].strip()
                if info["mime"].startswith("multipart/"):
                    info["error"] = f"unsupported content type {info['mime']}"
            elif name == "content-length" and val.isdigit():
                if int(val) > TELEGRAM_URL_MAXSIZE:
                    info["error"] = "attachment too large"

        def on_chunk(chunk: bytes) -> None:
            if len(buf) + len(chunk) > TELEGRAM_URL_MAXSIZE:
                info["error"] = info["error"] or "attachment too large"
            if info["error"]:
                buf.clear()
            else:
                buf.extend(chunk)

        try:
            resp = await self.transport.fetch(
                url, 10., header_callback=on_header, streaming_callback=on_chunk
            )
        except Exception as e:
            # an aborted download is raised even with raise_error=False
            raise Exception(info["error"] or e) from None
        if info["error"]:
            raise Exception(info["error"])
        if resp.error is not None:
            raise Exception(f"HTTP error: {resp.error}")
        return buf, info["mime"]

    async def _api(
        self, method: str, fields: Dict[str, Any],
        upload: Optional[Tuple[str, str, str, Data]] = None
    ) -> Tuple[int, str]:
        # Calls a Bot API method, logs a failure, returns the status code and
        # Telegram's error description
        kwargs: Dict[str, Any] = {}
        if upload is None:
            headers = {"Content-Type": "application/json"}
            kwargs["body"] = jsonw.dumps(fields)
        else:
            # multipart/form-data with the fields and a single file, the
            # file is written as is, without copying it into a request body
            bnd = uuid.uuid4().hex
            field, fname, mime, data = upload
            parts = [
                f'--{bnd}\r\nContent-Disposition: form-data; name="{name}"'
                f'\r\n\r\n{str(val).lower() if isinstance(val, bool) else val}'
                for name, val in fields.items()
            ]
            parts.append(
                f'--{bnd}\r\nContent-Disposition: form-data; name="{field}"; '
                f'filename="{fname}"\r\nContent-Type: {mime}\r\n\r\n'
            )
            head = "\r\n".join(parts).encode()
            tail = f"\r\n--{bnd}--\r\n".encode()
            headers = {
                "Content-Type": f"multipart/form-data; boundary={bnd}",
                "Content-Length": str(len(head) + len(data) + len(tail))
            }

            async def produce(write) -> None:
                for chunk in (head, memoryview(data), tail):
                    await write(chunk)
            kwargs["body_producer"] = produce
        try:
            resp = await self.transport.fetch(
                f"{self.transport.api_url}/bot{self.token}/{method}",
                15. if upload is None else 60.,
                method="POST", headers=headers, **kwargs
            )
            code = resp.code
            desc = "" if resp.error is None else str(resp.error)
            if code != 200:
                try:
                    desc = str(jsonw.loads(resp.body)["description"])
                except Exception:
                    pass
        except Exception as e:
            code, desc = 0, str(e) or type(e).__name__
        desc = desc.replace(self.token, "<token>")
        if code != 200:
            logging.warning(
                f"[notifier {self.name}]: Telegram {method} to chat "
                f"{fields['chat_id']} failed: {desc} (code {code})"
            )
        return code, desc


def load_component(config: ConfigHelper) -> Notifier:
    return Notifier(config)
