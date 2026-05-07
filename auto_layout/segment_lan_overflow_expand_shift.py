"""
После раскладки КБ/ТА: для network_segments в зонах dc/office (DMZ, INT-NET, …) —
объединённый абсолютный bbox содержимого сегмента (вершины с parent=segment_oid в цепочке)
плюс позиционирующие ячейки КБ/ТА с якорем LAN этого сегмента.

Если union выходит за текущий swimlane — увеличить width/height до union + PAD_PX по краям;
сегменты правее / ниже порога сдвинуть на дельту расширения. Дочерние mxCell сидят под
object id сегмента и суммируют координату с swimlane — сдвигается только swimlane и оверлеи.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set, Tuple
import xml.etree.ElementTree as ET

from auto_layout.dmz_segments_layout import _INTERIOR_OVERFLOW_ZONES
from auto_layout.kb_layout import (
    KB_SCHEMA,
    _absolute_bbox_vertex_cell,
    _absolute_xy_of_mx_cell_top_left,
    _kb_positioning_mx_cell,
    _parse_geom_xywh,
    _parse_network_connection,
    _pick_anchor_network_oid,
)
from auto_layout.segment_intrinsic_layout import location_on_page
from auto_layout.services_ta_layout import TA_SCHEMAS, _ta_positioning_mx_cell

NETWORKS_SCHEMA = 'seaf.company.ta.services.networks'
SEGMENTS_SCHEMA = 'seaf.company.ta.services.network_segments'

PAD_PX = 30
_NEIGHBOR_EPS = 3.0
_MAX_ROUNDS = 14


def _bbox_union(
    a: Optional[Tuple[float, float, float, float]],
    b: Optional[Tuple[float, float, float, float]],
) -> Optional[Tuple[float, float, float, float]]:
    if a is None:
        return b
    if b is None:
        return a
    return min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])


def _geom_shift_xy(geom: ET.Element, dx: float, dy: float) -> None:
    if (geom.get('relative') or '').strip() == '1':
        return
    try:
        x = float(geom.get('x') or 0)
        y = float(geom.get('y') or 0)
    except (TypeError, ValueError):
        return
    geom.set('x', str(int(round(x + dx))))
    geom.set('y', str(int(round(y + dy))))


def _segment_swimlane_mx(root: ET.Element, seg_oid: str) -> Optional[ET.Element]:
    obj = root.find(f".//object[@id='{seg_oid}']")
    if obj is None:
        return None
    for mx in obj.iter('mxCell'):
        if mx.get('vertex') == '1' and mx.get('edge') != '1':
            return mx
    return None


def _walk_vertices_under_parent_chain(root: ET.Element, root_parent_id: str) -> List[ET.Element]:
    out: List[ET.Element] = []
    q: List[str] = [root_parent_id]
    seen_q: Set[str] = {root_parent_id}
    while q:
        pid = q.pop(0)
        for mx in root.iter('mxCell'):
            if mx.get('parent') != pid:
                continue
            if mx.get('vertex') != '1' or mx.get('edge') == '1':
                continue
            out.append(mx)
            cid = mx.get('id')
            if cid and cid not in seen_q:
                seen_q.add(cid)
                q.append(cid)
    return out


def _lan_oids_in_segment(nets: Dict[str, Any], seg_oid: str, page_ids: Set[str]) -> Set[str]:
    found: Set[str] = set()
    for nid, nd in nets.items():
        if nid not in page_ids or not isinstance(nd, dict):
            continue
        if str(nd.get('type') or '').strip().upper() != 'LAN':
            continue
        sg = nd.get('segment')
        if sg == seg_oid or (isinstance(sg, list) and seg_oid in sg):
            found.add(str(nid))
    return found


def _overlay_cells_for_segment_lans(
    root: ET.Element,
    sd: Any,
    conf: Dict[str, Any],
    page_ids: Set[str],
    location_root: Optional[str],
    lan_in_seg: Set[str],
) -> List[ET.Element]:
    cells: List[ET.Element] = []

    def feed(schema: str, pos_fn: Any) -> None:
        try:
            rows = sd.get_object(conf['data_yaml_file'], schema)
        except Exception:
            return
        if not isinstance(rows, dict):
            return
        for oid, row in rows.items():
            if oid not in page_ids:
                continue
            nc_raw = row.get('network_connection') if isinstance(row, dict) else None
            nc_list = _parse_network_connection(nc_raw)
            anchor = _pick_anchor_network_oid(nc_list, page_ids, location_root)
            if not anchor or anchor not in lan_in_seg:
                continue
            pos_mx = pos_fn(root, str(oid))
            if pos_mx is not None:
                cells.append(pos_mx)

    feed(KB_SCHEMA, _kb_positioning_mx_cell)
    for sch in TA_SCHEMAS:
        feed(sch, _ta_positioning_mx_cell)
    return cells


def _union_content_bbox_segment(
    root: ET.Element,
    seg_oid: str,
    overlay_cells: List[ET.Element],
) -> Optional[Tuple[float, float, float, float]]:
    union_bb: Optional[Tuple[float, float, float, float]] = None
    for mx in _walk_vertices_under_parent_chain(root, seg_oid):
        bb = _absolute_bbox_vertex_cell(root, mx)
        union_bb = _bbox_union(union_bb, bb)
    for pos_mx in overlay_cells:
        bb = _absolute_bbox_vertex_cell(root, pos_mx)
        union_bb = _bbox_union(union_bb, bb)
    return union_bb


def _shift_kb_ta_overlays_for_lans(
    root: ET.Element,
    sd: Any,
    conf: Dict[str, Any],
    page_ids: Set[str],
    location_root: Optional[str],
    lan_in_seg: Set[str],
    dx: float,
    dy: float,
) -> None:
    if abs(dx) < 0.01 and abs(dy) < 0.01:
        return

    def feed(schema: str, pos_fn: Any) -> None:
        try:
            rows = sd.get_object(conf['data_yaml_file'], schema)
        except Exception:
            return
        if not isinstance(rows, dict):
            return
        for oid, row in rows.items():
            if oid not in page_ids:
                continue
            nc_raw = row.get('network_connection') if isinstance(row, dict) else None
            nc_list = _parse_network_connection(nc_raw)
            anchor = _pick_anchor_network_oid(nc_list, page_ids, location_root)
            if not anchor or anchor not in lan_in_seg:
                continue
            pos_mx = pos_fn(root, str(oid))
            if pos_mx is None:
                continue
            gj = pos_mx.find('mxGeometry')
            if gj is not None:
                _geom_shift_xy(gj, dx, dy)

    feed(KB_SCHEMA, _kb_positioning_mx_cell)
    for sch in TA_SCHEMAS:
        feed(sch, _ta_positioning_mx_cell)


def _shift_neighbor_segment_swimlane_only(
    root: ET.Element,
    sd: Any,
    conf: Dict[str, Any],
    page_ids: Set[str],
    location_root: Optional[str],
    nets: Dict[str, Any],
    sj: str,
    dx: float,
    dy: float,
) -> None:
    cmx_j = _segment_swimlane_mx(root, sj)
    gj = cmx_j.find('mxGeometry') if cmx_j is not None else None
    if gj is not None:
        _geom_shift_xy(gj, dx, dy)
    lan_sj = _lan_oids_in_segment(nets, sj, page_ids)
    _shift_kb_ta_overlays_for_lans(
        root, sd, conf, page_ids, location_root, lan_sj, dx, dy,
    )


def expand_segments_for_lan_overflow_and_shift_neighbors(
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

    diagram.go_to_diagram(diagram_name=page_name)
    root = diagram.current_root

    page_ids: Set[str] = set(diagram_ids_map.get(page_name) or [])
    location_root = location_roots[0] if len(location_roots) == 1 else None

    try:
        segments = sd.get_object(conf['data_yaml_file'], SEGMENTS_SCHEMA)
        nets = sd.get_object(conf['data_yaml_file'], NETWORKS_SCHEMA)
    except Exception:
        return
    if not isinstance(segments, dict) or not isinstance(nets, dict):
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

    def snapshot_seg_bounds() -> Dict[str, Tuple[float, float, float, float]]:
        bbmap: Dict[str, Tuple[float, float, float, float]] = {}
        for soid in seg_on_page:
            cmx = _segment_swimlane_mx(root, soid)
            if cmx is None:
                continue
            bb = _absolute_bbox_vertex_cell(root, cmx)
            if bb:
                bbmap[soid] = bb
        return bbmap

    def horizontal_pass() -> bool:
        progressed = False
        bb0 = snapshot_seg_bounds()
        ordered = sorted([s for s in seg_on_page if s in bb0], key=lambda s: bb0[s][0])
        for si in ordered:
            bounds_now = snapshot_seg_bounds()
            if si not in bounds_now:
                continue
            cmx_i = _segment_swimlane_mx(root, si)
            geom_i = cmx_i.find('mxGeometry') if cmx_i is not None else None
            if cmx_i is None or geom_i is None:
                continue
            t = _parse_geom_xywh(geom_i)
            if t is None:
                continue
            cw, ch = t[2], t[3]

            lan_set = _lan_oids_in_segment(nets, si, page_ids)
            overlays_i = _overlay_cells_for_segment_lans(
                root, sd, conf, page_ids, location_root, lan_set,
            )
            u_bb = _union_content_bbox_segment(root, si, overlays_i)
            if u_bb is None:
                continue

            seg_left = _absolute_xy_of_mx_cell_top_left(root, cmx_i)[0]

            need_w = float(u_bb[2]) - seg_left + float(PAD_PX)
            dw = max(0.0, need_w - float(cw))
            if dw <= 0.01:
                continue

            progressed = True
            si_right_old = bounds_now[si][2]

            geom_i.set('width', str(int(round(float(cw) + dw))))
            bounds_mid = snapshot_seg_bounds()
            si_bb_new = bounds_mid.get(si)
            if si_bb_new is None:
                continue
            dx_eff = si_bb_new[2] - si_right_old
            if dx_eff <= 0.01:
                continue

            for sj in seg_on_page:
                if sj == si:
                    continue
                bj = bounds_mid.get(sj)
                if not bj:
                    continue
                if bj[0] >= si_right_old - _NEIGHBOR_EPS:
                    _shift_neighbor_segment_swimlane_only(
                        root, sd, conf, page_ids, location_root, nets, sj, dx_eff, 0.0,
                    )
        return progressed

    def vertical_pass() -> bool:
        progressed = False
        bb0 = snapshot_seg_bounds()
        ordered = sorted([s for s in seg_on_page if s in bb0], key=lambda s: bb0[s][1])
        for si in ordered:
            bounds_now = snapshot_seg_bounds()
            if si not in bounds_now:
                continue
            cmx_i = _segment_swimlane_mx(root, si)
            geom_i = cmx_i.find('mxGeometry') if cmx_i is not None else None
            if cmx_i is None or geom_i is None:
                continue
            t = _parse_geom_xywh(geom_i)
            if t is None:
                continue
            _, ch = t[2], t[3]

            lan_set = _lan_oids_in_segment(nets, si, page_ids)
            overlays_i = _overlay_cells_for_segment_lans(
                root, sd, conf, page_ids, location_root, lan_set,
            )
            u_bb = _union_content_bbox_segment(root, si, overlays_i)
            if u_bb is None:
                continue

            seg_top = _absolute_xy_of_mx_cell_top_left(root, cmx_i)[1]

            need_h = float(u_bb[3]) - seg_top + float(PAD_PX)
            dh = max(0.0, need_h - float(ch))
            if dh <= 0.01:
                continue

            progressed = True
            si_bottom_old = bounds_now[si][3]

            geom_i.set('height', str(int(round(float(ch) + dh))))
            bounds_mid = snapshot_seg_bounds()
            si_bb_new = bounds_mid.get(si)
            if si_bb_new is None:
                continue
            dy_eff = si_bb_new[3] - si_bottom_old
            if dy_eff <= 0.01:
                continue

            for sj in seg_on_page:
                if sj == si:
                    continue
                bj = bounds_mid.get(sj)
                if not bj:
                    continue
                if bj[1] >= si_bottom_old - _NEIGHBOR_EPS:
                    _shift_neighbor_segment_swimlane_only(
                        root, sd, conf, page_ids, location_root, nets, sj, 0.0, dy_eff,
                    )
        return progressed

    for _round in range(_MAX_ROUNDS):
        progressed_h = horizontal_pass()
        progressed_v = vertical_pass()
        if not progressed_h and not progressed_v:
            break
