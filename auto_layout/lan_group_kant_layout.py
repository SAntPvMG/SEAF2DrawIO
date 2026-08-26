"""
Пунктирная скруглённая рамка по шаблону вокруг объектов, связанных с LAN в interior-сегменте:
сервисы КБ (слой 101), сервисы ТА (слой 102), User Devices — только те, у которых в данных
эта LAN в network_connection (для КБ/ТА якорь совпадает с kb_layout / services_TA_layout).

На каждую LAN в сегменте одна рамка: объединение абсолютных bbox объектов на canvas (для КБ/ТА —
только ячейка позиционирования на слое), затем асимметричное расширение: верхний левый
(min_x−30, min_y−30), нижний правый (max_x+20, max_y+40); координаты относительно swimlane
сегмента (parent=segment_oid) и обрезка по внутренней области сегмента. Обводка
KANT_STROKE_WIDTH, пунктир KANT_DASH_PATTERN.
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Optional, Set, Tuple
import xml.etree.ElementTree as ET

from auto_layout.dmz_segments_layout import _INTERIOR_OVERFLOW_ZONES
from auto_layout.kb_layout import (
    KB_SCHEMA,
    _absolute_bbox_vertex_cell,
    _absolute_xy_of_mx_cell_top_left,
    _canvas_bbox_for_oid,
    _kb_positioning_mx_cell,
    _parse_network_connection,
    _pick_anchor_network_oid,
)
from auto_layout.layout_pattern_modes import patterns_yaml_uses_interior_layout
from auto_layout.segment_intrinsic_layout import location_on_page
from auto_layout.services_ta_layout import TA_SCHEMAS, _ta_positioning_mx_cell

SEGMENTS_SCHEMA = 'seaf.company.ta.services.network_segments'
NETWORKS_SCHEMA = 'seaf.company.ta.services.networks'

USER_DEVICES_SCHEMA = 'seaf.company.ta.components.user_devices'

KANT_ID_PREFIX = 'lan_kant__'

# Отступ рамки от union bbox объектов (асимметрично)
KANT_OUTSET_MIN_X_PX = 30
KANT_OUTSET_MIN_Y_PX = 30
KANT_OUTSET_MAX_X_PX = 20
KANT_OUTSET_MAX_Y_PX = 40

KANT_STROKE_WIDTH = 1
KANT_DASH_PATTERN = '4 4'  # короче штрих и зазор, чем 8 8

_KANT_STYLE = (
    'rounded=1;dashed=1;'
    f'dashPattern={KANT_DASH_PATTERN};fillColor=none;strokeColor=#000000;'
    f'strokeWidth={KANT_STROKE_WIDTH};html=1;whiteSpace=wrap;pointerEvents=0;'
    'absoluteArcSize=1;arcSize=14'
)


def _segment_swimlane_mx(root: ET.Element, seg_oid: str) -> Optional[ET.Element]:
    obj = root.find(f".//object[@id='{seg_oid}']")
    if obj is None:
        return None
    fallback: Optional[ET.Element] = None
    for mx in obj.iter('mxCell'):
        if mx.get('vertex') != '1' or mx.get('edge') == '1':
            continue
        if mx.get('parent') == '001':
            return mx
        if fallback is None:
            fallback = mx
    return fallback


def _remove_existing_kant_cells(root: ET.Element) -> None:
    to_drop: List[Tuple[ET.Element, ET.Element]] = []
    for parent in root.iter():
        for child in list(parent):
            if child.tag != 'mxCell':
                continue
            cid = child.get('id') or ''
            if cid.startswith('lan_kant_'):
                to_drop.append((parent, child))
    for parent, child in to_drop:
        parent.remove(child)


def _first_root_child_index_for_segment_mxcells(root: ET.Element, seg_oid: str) -> Optional[int]:
    for i, ch in enumerate(root):
        if ch.tag == 'mxCell' and ch.get('parent') == seg_oid:
            return i
        if ch.tag == 'object':
            for mx in ch.iter('mxCell'):
                if mx.get('parent') == seg_oid:
                    return i
    return None


def _union_bbox(
    a: Optional[Tuple[float, float, float, float]],
    b: Optional[Tuple[float, float, float, float]],
) -> Optional[Tuple[float, float, float, float]]:
    if a is None:
        return b
    if b is None:
        return a
    return min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])


def _bbox_relative_to_swimlane(
    root: ET.Element,
    swimlane_mx: ET.Element,
    abs_bb: Tuple[float, float, float, float],
) -> Tuple[float, float, float, float]:
    ox, oy = _absolute_xy_of_mx_cell_top_left(root, swimlane_mx)
    return (
        abs_bb[0] - ox,
        abs_bb[1] - oy,
        abs_bb[2] - ox,
        abs_bb[3] - oy,
    )


def _union_kb_or_ta_row_bbox_for_anchor_lan(
    root: ET.Element,
    sd: Any,
    conf: Dict[str, Any],
    page_ids: Set[str],
    location_root: Optional[str],
    lan_oid: str,
    schema: str,
    pos_fn: Any,
    union_abs: Optional[Tuple[float, float, float, float]],
) -> Optional[Tuple[float, float, float, float]]:
    try:
        rows = sd.get_object(conf['data_yaml_file'], schema)
    except Exception:
        return union_abs
    if not isinstance(rows, dict):
        return union_abs
    for oid, row in rows.items():
        if oid not in page_ids:
            continue
        nc_raw = row.get('network_connection') if isinstance(row, dict) else None
        nc_list = _parse_network_connection(nc_raw)
        anchor = _pick_anchor_network_oid(nc_list, page_ids, location_root)
        if anchor != lan_oid:
            continue
        # Только ячейка позиционирования на слое КБ/ТА: иначе _canvas_bbox_for_oid
        # подхватывает «осиротевшие» группы (другой parent / старые координаты) и
        # раздувает пунктирную рамку за пределы сегмента ЦОД.
        pos_mx = pos_fn(root, str(oid))
        if pos_mx is None:
            continue
        bb = _absolute_bbox_vertex_cell(root, pos_mx)
        union_abs = _union_bbox(union_abs, bb)
    return union_abs


def _clamp_relative_bbox_to_swimlane(
    swimlane_mx: ET.Element,
    rx1: float,
    ry1: float,
    rx2: float,
    ry2: float,
    pad: float = 4.0,
) -> Optional[Tuple[float, float, float, float]]:
    """Обрезает локальный bbox рамки по внутренней области swimlane сегмента."""
    geom = swimlane_mx.find('mxGeometry')
    if geom is None:
        return rx1, ry1, rx2, ry2
    try:
        sw = float(geom.get('width') or 0)
        sh = float(geom.get('height') or 0)
    except (TypeError, ValueError):
        return rx1, ry1, rx2, ry2
    if sw < pad * 2 + 4 or sh < pad * 2 + 4:
        return None
    x1 = max(pad, min(rx1, sw - pad - 4.0))
    y1 = max(pad, min(ry1, sh - pad - 4.0))
    x2 = min(sw - pad, max(rx2, pad + 4.0))
    y2 = min(sh - pad, max(ry2, pad + 4.0))
    if x2 - x1 < 4.0 or y2 - y1 < 4.0:
        return None
    return x1, y1, x2, y2


def _union_kb_ta_ud_for_lan(
    root: ET.Element,
    sd: Any,
    conf: Dict[str, Any],
    page_ids: Set[str],
    location_root: Optional[str],
    seg_oid: str,
    lan_oid: str,
    location_roots: List[str],
) -> Optional[Tuple[float, float, float, float]]:
    union_abs: Optional[Tuple[float, float, float, float]] = None

    union_abs = _union_kb_or_ta_row_bbox_for_anchor_lan(
        root, sd, conf, page_ids, location_root, lan_oid,
        KB_SCHEMA, _kb_positioning_mx_cell, union_abs,
    )
    for sch in TA_SCHEMAS:
        union_abs = _union_kb_or_ta_row_bbox_for_anchor_lan(
            root, sd, conf, page_ids, location_root, lan_oid,
            sch, _ta_positioning_mx_cell, union_abs,
        )

    try:
        ud_rows = sd.get_object(conf['data_yaml_file'], USER_DEVICES_SCHEMA)
    except Exception:
        ud_rows = None
    if isinstance(ud_rows, dict):
        for oid, row in ud_rows.items():
            if oid not in page_ids:
                continue
            if not isinstance(row, dict):
                continue
            if not location_on_page(row.get('location'), location_roots):
                continue
            seg = row.get('segment')
            if seg:
                if isinstance(seg, str) and seg != seg_oid:
                    continue
                if isinstance(seg, list) and seg_oid not in seg:
                    continue
            nc_list = _parse_network_connection(row.get('network_connection'))
            if lan_oid not in nc_list:
                continue
            bb = _canvas_bbox_for_oid(root, str(oid))
            union_abs = _union_bbox(union_abs, bb)

    return union_abs


def _build_kant_mx_cell(kant_id: str, seg_oid: str, x: float, y: float, w: float, h: float) -> ET.Element:
    mx = ET.Element('mxCell')
    mx.set('id', kant_id)
    mx.set('value', '')
    mx.set('style', _KANT_STYLE)
    mx.set('vertex', '1')
    mx.set('parent', seg_oid)
    geom = ET.SubElement(mx, 'mxGeometry')
    geom.set('x', str(int(round(x))))
    geom.set('y', str(int(round(y))))
    geom.set('width', str(max(4, int(round(w)))))
    geom.set('height', str(max(4, int(round(h)))))
    geom.set('as', 'geometry')
    return mx


def place_lan_group_kant_cells(
    diagram: Any,
    sd: Any,
    conf: Dict[str, Any],
    page_name: str,
    diagram_ids_map: Dict[str, Any],
    patterns_yaml_path: str,
    location_roots: List[str],
) -> None:
    if not patterns_yaml_path or not patterns_yaml_uses_interior_layout(patterns_yaml_path):
        return

    diagram.go_to_diagram(diagram_name=page_name)
    root = diagram.current_root

    page_ids: Set[str] = set(diagram_ids_map.get(page_name) or [])

    try:
        segments = sd.get_object(conf['data_yaml_file'], SEGMENTS_SCHEMA)
        networks = sd.get_object(conf['data_yaml_file'], NETWORKS_SCHEMA)
    except Exception:
        return
    if not isinstance(segments, dict) or not isinstance(networks, dict):
        return

    seg_on_page = [
        str(soid)
        for soid, srow in segments.items()
        if isinstance(srow, dict)
        and soid in page_ids
        and location_on_page(srow.get('location'), location_roots)
        and str(srow.get('zone') or '') in _INTERIOR_OVERFLOW_ZONES
    ]
    if not seg_on_page:
        return

    location_root = location_roots[0] if len(location_roots) == 1 else None

    _remove_existing_kant_cells(root)

    for seg_oid in sorted(seg_on_page):
        swimlane_mx = _segment_swimlane_mx(root, seg_oid)
        if swimlane_mx is None:
            continue

        lans_in_seg = [
            nid
            for nid, nd in networks.items()
            if isinstance(nd, dict)
            and nd.get('type') == 'LAN'
            and nid in page_ids
            and (
                nd.get('segment') == seg_oid
                or (isinstance(nd.get('segment'), list) and seg_oid in nd.get('segment', []))
            )
        ]

        insert_at = _first_root_child_index_for_segment_mxcells(root, seg_oid)
        inserted = 0

        for lan_oid in lans_in_seg:
            union_raw = _union_kb_ta_ud_for_lan(
                root, sd, conf, page_ids, location_root, seg_oid, lan_oid, location_roots,
            )
            if union_raw is None:
                continue

            union_outset = (
                union_raw[0] - float(KANT_OUTSET_MIN_X_PX),
                union_raw[1] - float(KANT_OUTSET_MIN_Y_PX),
                union_raw[2] + float(KANT_OUTSET_MAX_X_PX),
                union_raw[3] + float(KANT_OUTSET_MAX_Y_PX),
            )

            rx1, ry1, rx2, ry2 = _bbox_relative_to_swimlane(root, swimlane_mx, union_outset)
            clamped = _clamp_relative_bbox_to_swimlane(swimlane_mx, rx1, ry1, rx2, ry2)
            if clamped is None:
                continue
            rx1, ry1, rx2, ry2 = clamped
            rw = rx2 - rx1
            rh = ry2 - ry1
            if rw < 1.0 or rh < 1.0:
                continue

            safe_lan = lan_oid.replace('.', '_').replace('/', '_')
            kant_id = f'{KANT_ID_PREFIX}{seg_oid.replace(".", "_")}__{safe_lan}'
            if len(kant_id) > 180:
                dig = hashlib.sha256(f'{seg_oid}\0{lan_oid}'.encode('utf-8')).hexdigest()[:20]
                kant_id = f'{KANT_ID_PREFIX}{dig}'

            kant_el = _build_kant_mx_cell(kant_id, seg_oid, rx1, ry1, rw, rh)
            if insert_at is None:
                root.append(kant_el)
            else:
                root.insert(insert_at + inserted, kant_el)
                inserted += 1
