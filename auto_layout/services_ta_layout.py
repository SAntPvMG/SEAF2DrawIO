"""
Раскладка сервисов ТА (слой Draw.IO «102», parent_id network_connection): справа от первого OID
в network_connection в зоне страницы; вертикально — напротив визуального bbox якоря (LAN);
до 4 в ряд, шаг 20 px.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple
import xml.etree.ElementTree as ET

from auto_layout.kb_layout import (
    LAN_TO_SERVICES_GAP_PX,
    _canvas_bbox_for_oid,
    _parse_network_connection,
    _pick_anchor_network_oid,
)

TA_LAYER_PARENT = '102'
_GAP_RIGHT = LAN_TO_SERVICES_GAP_PX
_ANCHOR_TOP_OFFSET_PX = 40
_GRID_GAP_PX = 20
_PER_ROW = 4

TA_SCHEMAS: Tuple[str, ...] = (
    'seaf.company.ta.services.compute_services',
    'seaf.company.ta.services.monitorings',
    'seaf.company.ta.services.backups',
    'seaf.company.ta.services.cluster_virtualizations',
    'seaf.company.ta.services.k8s',
)


def _ta_positioning_mx_cell(root: ET.Element, service_oid: str) -> Optional[ET.Element]:
    """Группа/иконка сервиса ТА под слоем 102 (или первый mxCell с parent=102 под object)."""
    el = root.find(f".//mxCell[@id='{service_oid}_0']")
    if el is not None and el.get('parent') == TA_LAYER_PARENT:
        return el
    el = root.find(f".//mxCell[@id='{service_oid}']")
    if el is not None and el.get('parent') == TA_LAYER_PARENT:
        return el
    for mx in root.findall('.//mxCell'):
        if mx.get('parent') != TA_LAYER_PARENT:
            continue
        mid = mx.get('id') or ''
        if mid == service_oid or mid.startswith(f'{service_oid}_'):
            return mx
    obj = root.find(f".//object[@id='{service_oid}']")
    if obj is not None:
        for mx in obj.iter('mxCell'):
            if mx.get('parent') == TA_LAYER_PARENT and mx.get('vertex') == '1':
                return mx
    return None


def _patterns_have_ta_services(doc: Dict[str, Any]) -> bool:
    for v in doc.values():
        if not isinstance(v, dict):
            continue
        if v.get('parent_id') != 'network_connection':
            continue
        sch = v.get('schema')
        if sch in TA_SCHEMAS:
            return True
    return False


def services_TA_layout(
    diagram: Any,
    sd: Any,
    conf: Dict[str, Any],
    page_name: str,
    diagram_ids_map: Dict[str, Any],
    patterns_yaml_path: str,
    location_roots: List[str],
) -> None:
    if not patterns_yaml_path.endswith(('office.yaml', 'dc.yaml')):
        return

    doc = sd.read_yaml_file(patterns_yaml_path) or {}
    if not _patterns_have_ta_services(doc):
        return

    diagram.go_to_diagram(diagram_name=page_name)
    root = diagram.current_root

    page_ids: Set[str] = set(diagram_ids_map.get(page_name) or [])
    location_root = location_roots[0] if len(location_roots) == 1 else None

    rows_by_oid: Dict[str, Dict[str, Any]] = {}
    for schema in TA_SCHEMAS:
        try:
            chunk = sd.get_object(conf['data_yaml_file'], schema)
        except Exception:
            continue
        if not isinstance(chunk, dict):
            continue
        for oid, row in chunk.items():
            if oid not in page_ids:
                continue
            soid = str(oid)
            if soid in rows_by_oid:
                continue
            rows_by_oid[soid] = row if isinstance(row, dict) else {}

    layout_items: List[Tuple[str, str, Tuple[float, float, float, float], float, float, ET.Element]] = []

    for service_oid in sorted(rows_by_oid.keys()):
        row = rows_by_oid[service_oid]
        nc_raw = row.get('network_connection')
        nc_list = _parse_network_connection(nc_raw)
        anchor = _pick_anchor_network_oid(nc_list, page_ids, location_root)
        if not anchor:
            continue
        anchor_bb = _canvas_bbox_for_oid(root, anchor)
        if anchor_bb is None:
            continue

        pos_mx = _ta_positioning_mx_cell(root, service_oid)
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

        layout_items.append((service_oid, anchor, anchor_bb, w, h, pos_mx))

    by_anchor: Dict[str, List[Tuple[str, str, Tuple[float, float, float, float], float, float, ET.Element]]] = defaultdict(list)
    for item in layout_items:
        by_anchor[item[1]].append(item)

    for _anchor_oid, items in by_anchor.items():
        # Полная высота сетки напротив якоря (LAN), чтобы блок стоял напротив полоски, а не уезжал вниз.
        n = len(items)
        n_rows = (n + _PER_ROW - 1) // _PER_ROW
        row_heights: List[float] = []
        for row_start in range(0, n, _PER_ROW):
            row_slice = items[row_start : row_start + _PER_ROW]
            row_heights.append(max(t[4] for t in row_slice))
        grid_h = sum(row_heights) + _GRID_GAP_PX * max(0, n_rows - 1)

        ax_left, ay_top, ax_right, ay_bottom = items[0][2]
        # Всегда центрируем сетку по вертикали относительно визуального bbox LAN («напротив»).
        y0 = (ay_top + ay_bottom) / 2.0 - grid_h / 2.0

        row_offset_y = 0.0
        for row_idx, row_start in enumerate(range(0, n, _PER_ROW)):
            row_slice = items[row_start : row_start + _PER_ROW]
            max_h = row_heights[row_idx]
            x_cursor = ax_right + _GAP_RIGHT
            iy = int(round(y0 + row_offset_y))

            for _sid, _an, _bb, w, h, pos_mx in row_slice:
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
                x_cursor += w + _GRID_GAP_PX

            row_offset_y += max_h + _GRID_GAP_PX
