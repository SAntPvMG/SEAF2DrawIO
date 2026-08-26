"""
Сегментирующие NGFW — центром на границу разделяемых сегментов.

apply_cross_segment_firewall_positions задаёт координаты на этапе intrinsic (от правого края
полоски «левой» LAN), но последующие пассы двигают зоны: расширение сегментов под контент,
сдвиг соседей, выравнивание INT-NET. Координата устаревает, и иконка оказывается внутри зоны
поверх сервисов (NGFW-07 уезжал в середину INT-NET).

Здесь по итоговой геометрии XML файрвол с сетями в двух сегментах ставится серединой на границу
между их прямоугольниками, а несколько файрволов одной границы — столбиком с зазором.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set, Tuple
import xml.etree.ElementTree as ET

from auto_layout.kb_layout import _absolute_bbox_vertex_cell
from auto_layout.segment_intrinsic_layout import (
    CROSS_FW_STACK_GAP,
    _match_any_field,
    all_connection_lan_oids_ordered,
    classify_band_component,
    connection_lans_span_multiple_segments,
    location_on_page,
    primary_network_segment_oid,
)
from auto_layout.segment_lan_overflow_expand_shift import (
    NETWORKS_SCHEMA,
    SEGMENTS_SCHEMA,
    _segment_swimlane_mx,
)

COMPONENTS_SCHEMA = 'seaf.company.ta.components.networks'
FIREWALL_TYPE = 'Межсетевой экран (файрвол)'

# Иконка не должна упираться в край общей части зон (подпись зоны, скругление рамки).
BOUNDARY_INSET_PX = 8
_EPS = 1.0


def _firewall_mx_cell(root: ET.Element, fw_oid: str) -> Optional[ET.Element]:
    obj = root.find(f".//object[@id='{fw_oid}']")
    if obj is None:
        return None
    for mx in obj.iter('mxCell'):
        if mx.get('vertex') == '1' and mx.get('edge') != '1':
            return mx
    return None


def _segment_pair_for_firewall(
    cdata: Dict[str, Any],
    lan_oids: List[str],
    networks: Dict[str, Any],
) -> Optional[Tuple[str, str]]:
    """Свой сегмент файрвола и первый чужой в порядке network_connection (иначе первые два)."""
    ordered: List[str] = []
    for lid in lan_oids:
        nd = networks.get(lid)
        if not isinstance(nd, dict):
            continue
        soid = primary_network_segment_oid(nd)
        if soid and soid not in ordered:
            ordered.append(soid)
    if len(ordered) < 2:
        return None
    own = cdata.get('segment')
    if isinstance(own, list):
        own = own[0] if own else None
    if own in ordered:
        other = next((s for s in ordered if s != own), None)
        return (own, other) if other else None
    return ordered[0], ordered[1]


def _midpoint_between_ranges(
    lo_a: float,
    hi_a: float,
    lo_b: float,
    hi_b: float,
) -> Optional[float]:
    """Середина зазора между непересекающимися интервалами (граница зон)."""
    if hi_a <= lo_b + _EPS:
        return (hi_a + lo_b) / 2.0
    if hi_b <= lo_a + _EPS:
        return (hi_b + lo_a) / 2.0
    return None


def _overlap_range(lo_a: float, hi_a: float, lo_b: float, hi_b: float) -> Optional[Tuple[float, float]]:
    lo, hi = max(lo_a, lo_b), min(hi_a, hi_b)
    return (lo, hi) if hi - lo > _EPS else None


def _clamped(value: float, size: float, limits: Optional[Tuple[float, float]]) -> float:
    if limits is None:
        return value
    lo = limits[0] + BOUNDARY_INSET_PX
    hi = limits[1] - BOUNDARY_INSET_PX - size
    if hi < lo:
        return (limits[0] + limits[1] - size) / 2.0
    return max(lo, min(value, hi))


def _boundary_target(
    fw_bb: Tuple[float, float, float, float],
    bb_a: Tuple[float, float, float, float],
    bb_b: Tuple[float, float, float, float],
) -> Optional[Tuple[str, float, float, float, Optional[Tuple[float, float]]]]:
    """
    (ось границы, координата границы, целевой x, целевой y, допустимый диапазон вдоль границы).

    'x' — зоны стоят рядом по горизонтали (основной случай), иконка центрируется по вертикальной
    линии стыка; 'y' — зоны одна под другой. Вложенные / пересекающиеся зоны пропускаем.
    """
    fw_w = fw_bb[2] - fw_bb[0]
    fw_h = fw_bb[3] - fw_bb[1]

    bx = _midpoint_between_ranges(bb_a[0], bb_a[2], bb_b[0], bb_b[2])
    if bx is not None:
        y_limits = _overlap_range(bb_a[1], bb_a[3], bb_b[1], bb_b[3])
        return 'x', bx, bx - fw_w / 2.0, _clamped(fw_bb[1], fw_h, y_limits), y_limits

    by = _midpoint_between_ranges(bb_a[1], bb_a[3], bb_b[1], bb_b[3])
    if by is not None:
        x_limits = _overlap_range(bb_a[0], bb_a[2], bb_b[0], bb_b[2])
        return 'y', by, _clamped(fw_bb[0], fw_w, x_limits), by - fw_h / 2.0, x_limits

    return None


def _stack_along_boundary(items: List[Dict[str, Any]]) -> None:
    """Разносит иконки одной границы вдоль неё, сохраняя порядок и не выходя за общую часть зон."""
    axis = items[0]['axis']
    key = 'y' if axis == 'x' else 'x'
    size_key = 'h' if axis == 'x' else 'w'
    items.sort(key=lambda it: (it[key], it['fw_oid']))
    prev_end: Optional[float] = None
    for it in items:
        if prev_end is not None:
            it[key] = max(it[key], prev_end + float(CROSS_FW_STACK_GAP))
        prev_end = it[key] + it[size_key]

    limits = items[0]['limits']
    if limits is None:
        return
    overflow = prev_end - (limits[1] - BOUNDARY_INSET_PX) if prev_end is not None else 0.0
    if overflow <= 0.0:
        return
    head_room = items[0][key] - (limits[0] + BOUNDARY_INSET_PX)
    shift = min(overflow, max(0.0, head_room))
    if shift <= 0.0:
        return
    for it in items:
        it[key] -= shift


def place_cross_segment_firewalls_on_boundary(
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

    patterns_doc = sd.read_yaml_file(patterns_yaml_path) or {}
    fpsu_pat = patterns_doc.get('fpsu') or {}
    specs = {
        'router': patterns_doc.get('router') or {},
        'firewall': patterns_doc.get('firewall') or {},
        'fpsu': fpsu_pat,
        'vpn': patterns_doc.get('vpn') or {},
        'switch': patterns_doc.get('switch') or {},
    }

    try:
        components = sd.get_object(conf['data_yaml_file'], COMPONENTS_SCHEMA)
        networks = sd.get_object(conf['data_yaml_file'], NETWORKS_SCHEMA)
        segments = sd.get_object(conf['data_yaml_file'], SEGMENTS_SCHEMA)
    except Exception:
        return
    if not isinstance(components, dict) or not isinstance(networks, dict) or not isinstance(segments, dict):
        return

    diagram.go_to_diagram(diagram_name=page_name)
    root = diagram.current_root
    page_ids: Set[str] = set(diagram_ids_map.get(page_name) or [])

    seg_bbox: Dict[str, Tuple[float, float, float, float]] = {}
    for soid in segments:
        if soid not in page_ids:
            continue
        cmx = _segment_swimlane_mx(root, str(soid))
        if cmx is None:
            continue
        bb = _absolute_bbox_vertex_cell(root, cmx)
        if bb:
            seg_bbox[str(soid)] = bb

    # Ключ включает пару зон: INET-EDGE/DMZ и EXT-WAN-EDGE/DMZ дают одну и ту же вертикаль,
    # но у них разные диапазоны Y — общий стек уводил бы иконки к чужой зоне.
    by_boundary: Dict[Tuple[str, int, Tuple[str, str]], List[Dict[str, Any]]] = {}
    for fw_oid, cdata in components.items():
        if not isinstance(cdata, dict) or fw_oid not in page_ids:
            continue
        if not location_on_page(cdata.get('location'), location_roots):
            continue
        if cdata.get('type') != FIREWALL_TYPE:
            continue
        if _match_any_field(cdata, fpsu_pat.get('any_field_regex') or {}):
            continue
        if classify_band_component(cdata, specs) != 'right':
            continue
        lan_oids = all_connection_lan_oids_ordered(cdata, networks)
        if len(lan_oids) < 2 or not connection_lans_span_multiple_segments(lan_oids, networks):
            continue
        pair = _segment_pair_for_firewall(cdata, lan_oids, networks)
        if pair is None or pair[0] not in seg_bbox or pair[1] not in seg_bbox:
            continue
        mx = _firewall_mx_cell(root, str(fw_oid))
        if mx is None:
            continue
        fw_bb = _absolute_bbox_vertex_cell(root, mx)
        if fw_bb is None:
            continue
        target = _boundary_target(fw_bb, seg_bbox[pair[0]], seg_bbox[pair[1]])
        if target is None:
            continue
        axis, coord, tx, ty, limits = target
        pair_key = (pair[0], pair[1]) if pair[0] <= pair[1] else (pair[1], pair[0])
        by_boundary.setdefault((axis, int(round(coord)), pair_key), []).append({
            'fw_oid': str(fw_oid),
            'mx': mx,
            'axis': axis,
            'limits': limits,
            'x': tx,
            'y': ty,
            'w': fw_bb[2] - fw_bb[0],
            'h': fw_bb[3] - fw_bb[1],
            'abs_x': fw_bb[0],
            'abs_y': fw_bb[1],
        })

    for items in by_boundary.values():
        _stack_along_boundary(items)
        for it in items:
            geom = it['mx'].find('mxGeometry')
            if geom is None:
                continue
            try:
                gx = float(geom.get('x') or 0)
                gy = float(geom.get('y') or 0)
            except (TypeError, ValueError):
                continue
            geom.set('x', str(int(round(gx + it['x'] - it['abs_x']))))
            geom.set('y', str(int(round(gy + it['y'] - it['abs_y']))))
