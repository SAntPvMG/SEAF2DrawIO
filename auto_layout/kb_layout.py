"""
Раскладка сервисов КБ (seaf.company.ta.services.kbs, слой «101»): справа от первого OID
network_connection в зоне страницы; по вертикали −80 px от верха якоря (выше верхней границы);
при нескольких КБ на один якорь — ряды по 4, интервал 20 px.
"""

from __future__ import annotations

import ast
import re
from collections import defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple
import xml.etree.ElementTree as ET

KB_LAYER_PARENT = '101'
KB_SCHEMA = 'seaf.company.ta.services.kbs'
# Зазор от полоски LAN до первого сервиса: пунктирная рамка lan_kant отступает влево
# на KANT_OUTSET_MIN_X_PX (30), плюс её собственный просвет до сети.
LAN_TO_SERVICES_GAP_PX = 45
_GAP_RIGHT = LAN_TO_SERVICES_GAP_PX
_ANCHOR_TOP_OFFSET_PX = -80
_KB_GAP_PX = 20
_KB_PER_ROW = 4


def _parse_network_connection(raw: Any) -> List[str]:
    if raw is None or raw == '':
        return []
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if x is not None and str(x).strip()]
    s = str(raw).strip()
    if s.startswith('['):
        try:
            v = ast.literal_eval(s)
            if isinstance(v, (list, tuple)):
                return [str(x).strip() for x in v if x is not None and str(x).strip()]
        except (SyntaxError, ValueError, TypeError):
            pass
    return [s]


def _dc_slug_from_location_root(location_root: Optional[str]) -> Optional[str]:
    if not location_root:
        return None
    m = re.search(r'(dc\d+)', location_root, re.I)
    return m.group(1).lower() if m else None


def _network_oid_matches_dc_zone(network_oid: str, dc_slug: Optional[str]) -> bool:
    """Зона ЦОД в OID сети: dc01/dc02 из корня страницы (офис — без фильтра)."""
    if not dc_slug:
        return True
    s = dc_slug.lower()
    n = network_oid.lower()
    return bool(re.search(r'(^|[._])' + re.escape(s) + r'([._]|$)', n))


def _resolve_mx_cell_parent(root: ET.Element, parent_attr: Optional[str]) -> Optional[ET.Element]:
    if not parent_attr or parent_attr == '0':
        return None
    cell = root.find(f".//mxCell[@id='{parent_attr}']")
    if cell is not None:
        return cell
    obj = root.find(f"./object[@id='{parent_attr}']")
    if obj is None:
        obj = root.find(f".//object[@id='{parent_attr}']")
    if obj is not None:
        return obj.find('mxCell')
    return None


def _parse_geom_xywh(geom: ET.Element) -> Optional[Tuple[float, float, float, float]]:
    if geom is None:
        return None
    if (geom.get('relative') or '').strip() == '1':
        return None
    try:
        x = float(geom.get('x') or 0)
        y = float(geom.get('y') or 0)
        w = float(geom.get('width') or 0)
        h = float(geom.get('height') or 0)
    except (TypeError, ValueError):
        return None
    if w <= 0 or h <= 0:
        return None
    return x, y, w, h


def _absolute_xy_of_mx_cell_top_left(root: ET.Element, mx_cell: ET.Element) -> Tuple[float, float]:
    ox, oy = 0.0, 0.0
    cur: Optional[ET.Element] = mx_cell
    seen = 0
    while cur is not None and seen < 500:
        seen += 1
        geom = cur.find('mxGeometry')
        if geom is not None:
            t = _parse_geom_xywh(geom)
            if t:
                ox += t[0]
                oy += t[1]
            else:
                try:
                    ox += float(geom.get('x') or 0)
                    oy += float(geom.get('y') or 0)
                except (TypeError, ValueError):
                    pass
        pid = cur.get('parent')
        if pid in (None, '0'):
            break
        cur = _resolve_mx_cell_parent(root, pid)
    return ox, oy


def _absolute_bbox_vertex_cell(root: ET.Element, mx_cell: ET.Element) -> Optional[Tuple[float, float, float, float]]:
    """Абсолютный bbox вершины. Для rotation=±90/270 оси меняются (вертикальные полоски LAN)."""
    geom = mx_cell.find('mxGeometry')
    t = _parse_geom_xywh(geom)
    if t is None:
        return None
    _, _, lw, lh = t
    ax, ay = _absolute_xy_of_mx_cell_top_left(root, mx_cell)
    ax1, ay1, ax2, ay2 = ax, ay, ax + lw, ay + lh

    style = mx_cell.get('style') or ''
    m = re.search(r'rotation=(-?\d+(?:\.\d+)?)', style)
    if not m:
        return ax1, ay1, ax2, ay2
    try:
        rot = abs(float(m.group(1))) % 360.0
    except (TypeError, ValueError):
        return ax1, ay1, ax2, ay2
    if abs(rot - 90.0) > 1.0 and abs(rot - 270.0) > 1.0:
        return ax1, ay1, ax2, ay2

    cx = (ax1 + ax2) / 2.0
    cy = (ay1 + ay2) / 2.0
    hw = (ax2 - ax1) / 2.0
    hh = (ay2 - ay1) / 2.0
    # 90/270: визуально ширина↔высота вокруг центра
    return cx - hh, cy - hw, cx + hh, cy + hw


def _canvas_bbox_for_oid(root: ET.Element, oid: str) -> Optional[Tuple[float, float, float, float]]:
    obj = root.find(f".//object[@id='{oid}']")
    candidates: List[ET.Element] = []
    if obj is not None:
        for c in obj.iter('mxCell'):
            if c.get('vertex') == '1' and c.get('edge') != '1':
                candidates.append(c)
    mx_top = root.find(f".//mxCell[@id='{oid}']")
    if mx_top is not None and mx_top.get('vertex') == '1':
        candidates.insert(0, mx_top)

    bbox: Optional[Tuple[float, float, float, float]] = None
    for c in candidates:
        bb = _absolute_bbox_vertex_cell(root, c)
        if bb is None:
            continue
        if bbox is None:
            bbox = bb
        else:
            bbox = (
                min(bbox[0], bb[0]),
                min(bbox[1], bb[1]),
                max(bbox[2], bb[2]),
                max(bbox[3], bb[3]),
            )
    return bbox


def _pick_anchor_network_oid(
    nc_list: List[str],
    page_ids: Set[str],
    location_root: Optional[str],
) -> Optional[str]:
    """Первый в исходном списке network_connection: есть на странице и в зоне этого ЦОД (dc01/dc02)."""
    slug = _dc_slug_from_location_root(location_root)
    for nid in nc_list:
        if nid not in page_ids:
            continue
        if not _network_oid_matches_dc_zone(nid, slug):
            continue
        return nid
    return None


def _kb_positioning_mx_cell(root: ET.Element, kb_oid: str) -> Optional[ET.Element]:
    el = root.find(f".//mxCell[@id='{kb_oid}_0']")
    if el is not None and el.get('parent') == KB_LAYER_PARENT:
        return el
    el = root.find(f".//mxCell[@id='{kb_oid}']")
    if el is not None and el.get('parent') == KB_LAYER_PARENT:
        return el
    for mx in root.findall('.//mxCell'):
        if mx.get('parent') != KB_LAYER_PARENT:
            continue
        mid = mx.get('id') or ''
        if mid == kb_oid or mid.startswith(f'{kb_oid}_'):
            return mx
    obj = root.find(f".//object[@id='{kb_oid}']")
    if obj is not None:
        for mx in obj.iter('mxCell'):
            if mx.get('parent') == KB_LAYER_PARENT and mx.get('vertex') == '1':
                return mx
    return None


def kb_layout(
    diagram: Any,
    sd: Any,
    conf: Dict[str, Any],
    page_name: str,
    diagram_ids_map: Dict[str, Any],
    patterns_yaml_path: str,
    location_roots: List[str],
) -> None:
    """
    КБ (слой 101) справа от первого подходящего network_connection на странице (зона ЦОД dc01/dc02).
    По вертикали смещение −80 px от верхней границы якорной сети; несколько КБ на один якорь —
    до 4 в ряд, интервал 20 px (между рядами тоже 20 px относительно высоты ряда).
    """
    if not patterns_yaml_path.endswith(('office.yaml', 'dc.yaml')):
        return

    doc = sd.read_yaml_file(patterns_yaml_path) or {}
    has_kb_pattern = any(
        isinstance(v, dict)
        and v.get('schema') == KB_SCHEMA
        and v.get('parent_id') == 'network_connection'
        for v in doc.values()
    )
    if not has_kb_pattern:
        return

    diagram.go_to_diagram(diagram_name=page_name)
    root = diagram.current_root

    try:
        kb_rows = sd.get_object(conf['data_yaml_file'], KB_SCHEMA)
    except Exception:
        kb_rows = {}

    page_ids = set(diagram_ids_map.get(page_name) or [])
    location_root = location_roots[0] if len(location_roots) == 1 else None

    layout_items: List[Tuple[str, str, Tuple[float, float, float, float], float, float, ET.Element]] = []

    for kb_oid, row in sorted(kb_rows.items(), key=lambda kv: str(kv[0])):
        if kb_oid not in page_ids:
            continue
        nc_raw = row.get('network_connection') if isinstance(row, dict) else None
        nc_list = _parse_network_connection(nc_raw)
        anchor = _pick_anchor_network_oid(nc_list, page_ids, location_root)
        if not anchor:
            continue
        anchor_bb = _canvas_bbox_for_oid(root, anchor)
        if anchor_bb is None:
            continue

        pos_mx = _kb_positioning_mx_cell(root, kb_oid)
        if pos_mx is None:
            continue
        geom = pos_mx.find('mxGeometry')
        if geom is None:
            continue

        try:
            w = float(geom.get('width') or 40)
            h = float(geom.get('height') or 40)
        except (TypeError, ValueError):
            w, h = 40.0, 40.0

        layout_items.append((kb_oid, anchor, anchor_bb, w, h, pos_mx))

    by_anchor: Dict[str, List[Tuple[str, str, Tuple[float, float, float, float], float, float, ET.Element]]] = defaultdict(list)
    for item in layout_items:
        by_anchor[item[1]].append(item)

    for _anchor_oid, items in by_anchor.items():
        row_offset_y = 0.0
        for row_start in range(0, len(items), _KB_PER_ROW):
            row_slice = items[row_start : row_start + _KB_PER_ROW]
            max_h = max(t[4] for t in row_slice)
            _, ay_top, ax_right, _ = row_slice[0][2]
            x_cursor = ax_right + _GAP_RIGHT
            iy = int(round(ay_top + _ANCHOR_TOP_OFFSET_PX + row_offset_y))

            for _kb_oid, _an, _bb, w, h, pos_mx in row_slice:
                geom = pos_mx.find('mxGeometry')
                if geom is None:
                    continue
                ix = int(round(x_cursor))
                geom.set('x', str(ix))
                geom.set('y', str(iy))
                if w > 0:
                    geom.set('width', str(int(round(w))))
                if h > 0:
                    geom.set('height', str(int(round(h))))
                x_cursor += w + _KB_GAP_PX

            row_offset_y += max_h + _KB_GAP_PX
