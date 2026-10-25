# (C) 2026 ghzserg https://github.com/ghzserg/zmod

import os
import re
import json
import time
import shutil
import uuid
import logging
import zipfile
import xml.etree.ElementTree as ET
from PIL import Image
from typing import Optional, List, Dict, Any, Callable

COPY_CHUNK = 256 * 1024
TMP_DIR_NAME = "tmp"
STALE_TMP_SECONDS = 600
GCODE_ONLY_KEYS = ("thumbnails", "gcode_start_byte", "gcode_end_byte")


class ThreeMFParser:
    def __init__(
        self,
        file_path: str,
        tmp_root: Optional[str] = None,
        gcode_analyzer: Optional[Callable[[str], Dict[str, Any]]] = None,
        plate_index: Optional[int] = None,
    ):
        self.path = file_path
        self.tmp_root = tmp_root or os.path.dirname(file_path)
        self.gcode_analyzer = gcode_analyzer
        try:
            pi = int(plate_index) if plate_index is not None else 1
        except (ValueError, TypeError):
            pi = 1
        pi = pi if pi > 0 else 1
        self.requested_plate = pi
        # Нарезанные платы = те, у которых есть непустой Metadata/plate_N.gcode
        self.sliced_plates: List[int] = self._list_sliced_plates()
        if self.sliced_plates and pi not in self.sliced_plates:
            logging.info(
                "3mf: plate %d is not sliced in %s, using plate %d",
                pi, os.path.basename(file_path), self.sliced_plates[0]
            )
            pi = self.sliced_plates[0]
        elif not self.sliced_plates:
            logging.warning(
                "3mf: no sliced plates found in %s", os.path.basename(file_path)
            )
        self.plate_index = pi
        self._plate_xml_loaded = False
        self._cached_plate_xml: Optional[ET.Element] = None
        self._cached_gcode_meta: Optional[Dict[str, Any]] = None

    def _list_sliced_plates(self) -> List[int]:
        found = set()
        try:
            with zipfile.ZipFile(self.path, 'r') as zf:
                for info in zf.infolist():
                    m = re.fullmatch(
                        r"Metadata/plate_(\d+)\.gcode", info.filename,
                        re.IGNORECASE
                    )
                    if m and info.file_size > 0:
                        found.add(int(m.group(1)))
        except Exception:
            logging.exception("3mf: unable to list plates")
        return sorted(found)

    @staticmethod
    def _plate_element_index(plate: ET.Element) -> Optional[int]:
        for meta in plate.findall("metadata"):
            if meta.attrib.get("key") in ("index", "plater_id"):
                try:
                    return int(meta.attrib.get("value", ""))
                except ValueError:
                    return None
        return None

    def _select_plate_element(self, root: ET.Element) -> Optional[ET.Element]:
        plates = root.findall("plate")
        if not plates:
            return root
        for p in plates:
            if self._plate_element_index(p) == self.plate_index:
                return p
        if len(plates) == 1 and self._plate_element_index(plates[0]) is None:
            return plates[0]
        return None

    def get_plate_xml(self) -> Optional[ET.Element]:
        if self._plate_xml_loaded:
            return self._cached_plate_xml
        self._plate_xml_loaded = True
        try:
            with zipfile.ZipFile(self.path, 'r') as zf:
                file_list = zf.namelist()
                target_cfg = "Metadata/plate_" + str(self.plate_index) + ".config"
                xml_data = b""
                select = False
                if target_cfg in file_list:
                    xml_data = zf.read(target_cfg)
                elif 'Metadata/slice_info.config' in file_list:
                    xml_data = zf.read('Metadata/slice_info.config')
                    select = True
                if xml_data:
                    root = ET.fromstring(xml_data)
                    self._cached_plate_xml = (
                        self._select_plate_element(root) if select else root
                    )
        except Exception:
            logging.exception("3mf: unable to read plate config")
        return self._cached_plate_xml

    def _plate_image_regexes(self):
        n = self.plate_index
        full = re.compile(
            rf"^Metadata/plate_{n}\.(?:png|jpg|jpeg)$", re.IGNORECASE
        )
        small = re.compile(
            rf"^Metadata/plate_{n}_\w+\.(?:png|jpg|jpeg)$", re.IGNORECASE
        )
        return full, small

    def _image_belongs_to_plate(self, name: str) -> bool:
        nums = re.findall(r"_(\d+)(?=[_.])", os.path.basename(name))
        return not nums or str(self.plate_index) in nums

    def parse_thumbnails(self) -> Optional[List[Dict[str, Any]]]:
        parsed_matches: List[Dict[str, Any]] = []
        thumb_dir = os.path.join(os.path.dirname(self.path), ".thumbs")
        if not os.path.exists(thumb_dir):
            try:
                os.mkdir(thumb_dir)
            except Exception:
                return None
        thumb_base = os.path.splitext(os.path.basename(self.path))[0]
        try:
            with zipfile.ZipFile(self.path, 'r') as zf:
                file_list = zf.namelist()
                full_re, small_re = self._plate_image_regexes()
                images = [
                    f for f in file_list
                    if f.startswith("Metadata/")
                    and (full_re.match(f) or small_re.match(f))
                ]
                if not images:
                    # Любые картинки, но не принадлежащие другим платам
                    images = [
                        f for f in file_list
                        if f.startswith("Metadata/")
                        and f.lower().endswith((".png", ".jpg", ".jpeg"))
                        and self._image_belongs_to_plate(f)
                    ]
                for img_internal_path in images:
                    internal_name = os.path.basename(img_internal_path)
                    thumb_name = thumb_base + "-" + internal_name
                    thumb_path = os.path.join(thumb_dir, thumb_name)
                    rel_thumb_path = os.path.join(".thumbs", thumb_name)
                    img_data = zf.read(img_internal_path)
                    with open(thumb_path, "wb") as f:
                        f.write(img_data)
                    try:
                        with Image.open(thumb_path) as im:
                            w, h = im.width, im.height
                    except Exception:
                        w, h = 300, 300
                    parsed_matches.append({
                        'width': w, 'height': h,
                        'size': os.path.getsize(thumb_path),
                        'relative_path': rel_thumb_path,
                    })
        except Exception:
            pass
        return parsed_matches if parsed_matches else None

    def parse_estimated_time(self) -> Optional[float]:
        root = self.get_plate_xml()
        if root is not None:
            for meta in root.findall(".//metadata"):
                if meta.attrib.get("key") == "prediction":
                    val = meta.attrib.get("value")
                    return float(val) if val else None
        return None

    def parse_filament_weight_total(self) -> Optional[float]:
        root = self.get_plate_xml()
        if root is not None:
            for meta in root.findall(".//metadata"):
                if meta.attrib.get("key") == "weight":
                    val = meta.attrib.get("value")
                    return float(val) if val else None
        return None

    def parse_printer_model(self) -> Optional[str]:
        root = self.get_plate_xml()
        if root is not None:
            for meta in root.findall(".//metadata"):
                if meta.attrib.get("key") == "printer_model_id":
                    return meta.attrib.get("value")
        return None

    def parse_nozzle_diameter(self) -> Optional[float]:
        root = self.get_plate_xml()
        if root is not None:
            for meta in root.findall(".//metadata"):
                if meta.attrib.get("key") == "nozzle_diameters":
                    val = meta.attrib.get("value")
                    if val:
                        return float(val.split(",")[0])
        return None

    def parse_filament_type(self) -> Optional[str]:
        root = self.get_plate_xml()
        if root is not None:
            types = [
                fil.attrib.get("type", "Unknown").strip()
                for fil in root.findall(".//filament")
            ]
            if types:
                return json.dumps(types) if len(types) > 1 else types[0]
        return None

    def parse_filament_colors(self) -> Optional[List[str]]:
        root = self.get_plate_xml()
        if root is not None:
            colors = [
                fil.attrib.get("color", "").strip()
                for fil in root.findall(".//filament")
                if fil.attrib.get("color")
            ]
            if colors:
                return colors
        return None

    def _find_plate_gcode(self, zf: zipfile.ZipFile) -> Optional[str]:
        names = zf.namelist()
        target = "Metadata/plate_" + str(self.plate_index) + ".gcode"
        for n in names:
            if n.lower() == target.lower():
                return n
        if self.sliced_plates:
            return None
        plates = []
        for n in names:
            m = re.fullmatch(r"Metadata/plate_(\d+)\.gcode", n, re.IGNORECASE)
            if m:
                plates.append((int(m.group(1)), n))
        if plates:
            return min(plates)[1]
        others = sorted(n for n in names if n.lower().endswith(".gcode"))
        return others[0] if others else None

    def _cleanup_stale_tmp(self, tmp_dir: str) -> None:
        now = time.time()
        try:
            for name in os.listdir(tmp_dir):
                fp = os.path.join(tmp_dir, name)
                try:
                    if (
                        os.path.isfile(fp)
                        and now - os.path.getmtime(fp) > STALE_TMP_SECONDS
                    ):
                        os.remove(fp)
                except OSError:
                    pass
        except OSError:
            pass

    def get_gcode_metadata(self) -> Dict[str, Any]:
        if self._cached_gcode_meta is not None:
            return self._cached_gcode_meta
        self._cached_gcode_meta = {}
        if self.gcode_analyzer is None:
            return self._cached_gcode_meta
        tmp_dir = os.path.join(self.tmp_root, TMP_DIR_NAME)
        tmp_path: Optional[str] = None
        try:
            with zipfile.ZipFile(self.path, "r") as zf:
                inner = self._find_plate_gcode(zf)
                if inner is None:
                    return self._cached_gcode_meta
                os.makedirs(tmp_dir, exist_ok=True)
                self._cleanup_stale_tmp(tmp_dir)
                tmp_path = os.path.join(
                    tmp_dir, uuid.uuid4().hex + ".3mfgc"
                )
                with zf.open(inner) as src, open(tmp_path, "wb") as dst:
                    shutil.copyfileobj(src, dst, COPY_CHUNK)
            meta = self.gcode_analyzer(tmp_path)
            self._cached_gcode_meta = {
                k: v for k, v in meta.items() if k not in GCODE_ONLY_KEYS
            }
        except Exception:
            logging.exception(
                "3mf: unable to read gcode of plate " + str(self.plate_index)
            )
        finally:
            if tmp_path is not None:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
        return self._cached_gcode_meta
