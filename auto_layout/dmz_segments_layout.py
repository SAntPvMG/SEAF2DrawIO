"""
Раскладка DMZ и абсолютные координаты сетки зон (office.yaml и dc.yaml — одна и та же модель).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from auto_layout.layout_pattern_modes import patterns_yaml_uses_interior_layout
from auto_layout.segment_intrinsic_layout import (
    INET_EXT_VERTICAL_GAP,
    INT_SEGMENT_GAP_BOTTOM,
    INT_SEGMENT_GAP_RIGHT,
    SEGMENT_GAP,
    apply_cross_segment_firewall_positions,
    compute_intrinsic_band_layout,
    effective_segment_height,
    effective_segment_width,
    location_on_page,
    segment_rect_for_zone,
)

DMZ_ZONES = frozenset({'DMZ'})

_RIGHT_OF_STACK_ZONES = frozenset({'INT-NET', 'INT-SECURITY-NET'})

# Сегменты interior-сетки: проверка bbox дочерних объектов и расширение контейнера + сдвиг соседей справа
_INTERIOR_OVERFLOW_ZONES = frozenset({'DMZ', 'INT-NET', 'INT-SECURITY-NET', 'INT-WAN-EDGE'})


def _oid_zone_on_page(
    segments: Dict[str, Any],
    page_roots: List[str],
    zone: str,
) -> Optional[str]:
    oids = _all_oids_zone_on_page(segments, page_roots, zone)
    return oids[0] if oids else None


def _all_oids_zone_on_page(
    segments: Dict[str, Any],
    page_roots: List[str],
    zone: str,
) -> List[str]:
    return [
        oid
        for oid, seg in segments.items()
        if isinstance(seg, dict)
        and seg.get('zone') == zone
        and location_on_page(seg.get('location'), page_roots)
    ]


def _segment_child_oids_for_bbox(
    seg_oid: str,
    networks: Dict[str, Any],
    components: Dict[str, Any],
    page_roots: List[str],
) -> List[str]:
    """OID сетей и компонентов, принадлежащих сегменту (данные segment)."""
    out: List[str] = []
    for nid, nd in networks.items():
        if not isinstance(nd, dict):
            continue
        if not location_on_page(nd.get('location'), page_roots):
            continue
        sg = nd.get('segment')
        if sg == seg_oid or (isinstance(sg, list) and seg_oid in sg):
            out.append(str(nid))
    for cid, cd in components.items():
        if not isinstance(cd, dict):
            continue
        if not location_on_page(cd.get('location'), page_roots):
            continue
        sg_c = cd.get('segment')
        if sg_c != seg_oid and not (isinstance(sg_c, list) and seg_oid in sg_c):
            continue
        out.append(str(cid))
    return out


def _segment_template_wh(seg_oid: str, segments: Dict[str, Any], patterns_doc: Dict[str, Any]) -> Tuple[int, int]:
    seg = segments.get(seg_oid) or {}
    z = str(seg.get('zone') or '')
    rect = segment_rect_for_zone(patterns_doc, z)
    if not rect:
        return 400, 300
    return int(rect[2]), int(rect[3])


def expand_interior_segments_from_child_bbox(
    positions: Dict[str, Dict[str, Any]],
    segment_size: Dict[str, Dict[str, Any]],
    segments: Dict[str, Any],
    networks: Dict[str, Any],
    components: Dict[str, Any],
    page_roots: List[str],
    patterns_doc: Dict[str, Any],
) -> bool:
    """
    Если объединённый bbox дочерних объектов (координаты относительно сегмента) выходит за
    текущие segment_size с учётом INT_SEGMENT_GAP_* — увеличить w/h сегмента.
    Возвращает True, если хоть один размер вырос.
    """
    grew = False
    for seg_oid, seg in segments.items():
        if not isinstance(seg, dict):
            continue
        if seg.get('zone') not in _INTERIOR_OVERFLOW_ZONES:
            continue
        if not location_on_page(seg.get('location'), page_roots):
            continue
        kids = _segment_child_oids_for_bbox(seg_oid, networks, components, page_roots)
        with_pos = [o for o in kids if o in positions]
        if not with_pos:
            continue
        max_r = max(int(positions[o]['x']) + int(positions[o]['w']) for o in with_pos)
        max_b = max(int(positions[o]['y']) + int(positions[o]['h']) for o in with_pos)
        tw, th = _segment_template_wh(seg_oid, segments, patterns_doc)
        sz = segment_size.setdefault(seg_oid, {})
        cur_w = int(sz.get('w', tw))
        cur_h = int(sz.get('h', th))
        need_w = int(max_r + INT_SEGMENT_GAP_RIGHT)
        need_h = int(max_b + INT_SEGMENT_GAP_BOTTOM)
        if need_w > cur_w:
            sz['w'] = need_w
            grew = True
        if need_h > cur_h:
            sz['h'] = need_h
            grew = True
    return grew


def dmz_segments_layout(
    sd: Any,
    conf: Dict[str, Any],
    page_name: str,
    page_roots: List[str],
    patterns_yaml_path: str,
    wan_layout_cache: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    :param wan_layout_cache: результат edge_segments_layout (positions / segment_size по зонам).
    """
    empty: Dict[str, Any] = {
        'positions': {},
        'segment_size': {},
        'segment_origin': {},
        'cross_segment_firewall_oids': frozenset(),
    }
    if page_name == 'Main Schema' or not page_roots:
        return empty

    is_interior = patterns_yaml_uses_interior_layout(patterns_yaml_path)
    wan_layout_cache = wan_layout_cache or {}

    if is_interior:
        positions = dict(wan_layout_cache.get('positions') or {})
        segment_size = dict(wan_layout_cache.get('segment_size') or {})
    else:
        intrinsic = compute_intrinsic_band_layout(DMZ_ZONES, sd, conf, page_roots, patterns_yaml_path)
        positions = intrinsic.get('positions') or {}
        segment_size = intrinsic.get('segment_size') or {}

    segment_origin: Dict[str, Dict[str, int]] = {}

    try:
        merged = sd.read_and_merge_yaml(conf.get('data_yaml_file'))
        segments = merged.get('seaf.company.ta.services.network_segments') or {}
        networks = merged.get('seaf.company.ta.services.networks') or {}
        components = merged.get('seaf.company.ta.components.networks') or {}
    except Exception:
        return dict(empty)

    patterns_doc = sd.read_yaml_file(patterns_yaml_path) or {}

    inet_rect = segment_rect_for_zone(patterns_doc, 'INET-EDGE')
    wan_rect = segment_rect_for_zone(patterns_doc, 'INT-WAN-EDGE')
    dmz_rect = segment_rect_for_zone(patterns_doc, 'DMZ')
    ext_tpl = segment_rect_for_zone(patterns_doc, 'EXT-WAN-EDGE')
    int_net_tpl = segment_rect_for_zone(patterns_doc, 'INT-NET')
    int_sec_tpl = segment_rect_for_zone(patterns_doc, 'INT-SECURITY-NET')
    wan_edge_tpl = segment_rect_for_zone(patterns_doc, 'INT-WAN-EDGE')

    if not dmz_rect:
        return {
            'positions': positions,
            'segment_size': segment_size,
            'segment_origin': segment_origin,
            'cross_segment_firewall_oids': frozenset(),
        }

    dmz_dx, dmz_dy, dmz_dw, dmz_dh = dmz_rect

    dmz_oids_on_page = [
        oid for oid, seg in segments.items()
        if isinstance(seg, dict)
        and seg.get('zone') == 'DMZ'
        and location_on_page(seg.get('location'), page_roots)
    ]

    # ---------- interior (office.yaml / dc.yaml): полная сетка зон ----------
    # INT-SECURITY-NET в шаблоне не обязателен: иначе при его отсутствии не применялись pull_shift и segment_origin.
    if is_interior and inet_rect and ext_tpl and int_net_tpl and wan_edge_tpl:
        ix, iy, iw_t, ih_t = inet_rect
        inet_oids = _all_oids_zone_on_page(segments, page_roots, 'INET-EDGE')
        ext_oids = _all_oids_zone_on_page(segments, page_roots, 'EXT-WAN-EDGE')
        int_net_oid = _oid_zone_on_page(segments, page_roots, 'INT-NET')
        int_sec_oid = _oid_zone_on_page(segments, page_roots, 'INT-SECURITY-NET')
        int_wan_oids = _all_oids_zone_on_page(segments, page_roots, 'INT-WAN-EDGE')
        _, _, ew_t, eh_t = ext_tpl
        _, _, wan_w_t, wan_h_t = wan_edge_tpl
        has_dmz_data = bool(dmz_oids_on_page)

        def _refresh_interior_grid() -> None:
            inh_loc = (
                max(
                    (effective_segment_height(o, ih_t, wan_layout_cache) for o in inet_oids),
                    default=ih_t,
                )
                if inet_oids
                else ih_t
            )
            inw_loc = (
                max(
                    (effective_segment_width(o, iw_t, wan_layout_cache) for o in inet_oids),
                    default=iw_t,
                )
                if inet_oids
                else iw_t
            )

            exh_loc = (
                max(
                    (effective_segment_height(o, eh_t, wan_layout_cache) for o in ext_oids),
                    default=eh_t,
                )
                if ext_oids
                else eh_t
            )
            exw_loc = (
                max(
                    (effective_segment_width(o, ew_t, wan_layout_cache) for o in ext_oids),
                    default=ew_t,
                )
                if ext_oids
                else ew_t
            )

            left_col_w_loc = max(inw_loc, exw_loc)

            if int_wan_oids:
                wan_w_natural_loc = max(
                    int(segment_size.get(o, {}).get('w', wan_w_t)) for o in int_wan_oids
                )
            else:
                wan_w_natural_loc = int(wan_w_t)

            dmz_w_tpl_loc = int(dmz_dw)
            dmz_h_eff_loc = int(dmz_dh)
            for oid in dmz_oids_on_page:
                dmz_w_tpl_loc = max(dmz_w_tpl_loc, int(segment_size.get(oid, {}).get('w', dmz_dw)))
                dmz_h_eff_loc = max(
                    dmz_h_eff_loc,
                    effective_segment_height(oid, int(dmz_dh), wan_layout_cache),
                )

            if not has_dmz_data:
                pull_shift_loc = max(0, dmz_w_tpl_loc - wan_w_natural_loc)
                ix_eff_loc = ix + pull_shift_loc
                dmz_w_eff_loc = wan_w_natural_loc
                if int_wan_oids:
                    for _iw in int_wan_oids:
                        sz = segment_size.setdefault(_iw, {})
                        need_w = int(sz.get('w', wan_w_t))
                        sz['w'] = max(int(left_col_w_loc), need_w)
                    wan_w_eff_loc = max(int(segment_size[o]['w']) for o in int_wan_oids)
                else:
                    wan_w_eff_loc = int(wan_w_t)
                dmz_h_for_wan_loc = 0
            else:
                ix_eff_loc = ix
                dmz_w_eff_loc = dmz_w_tpl_loc
                if int_wan_oids:
                    for _iw in int_wan_oids:
                        sz = segment_size.setdefault(_iw, {})
                        need_w = int(sz.get('w', wan_w_t))
                        sz['w'] = max(int(dmz_w_eff_loc), need_w)
                    wan_w_eff_loc = max(int(segment_size[o]['w']) for o in int_wan_oids)
                else:
                    wan_w_eff_loc = int(wan_w_t)
                dmz_h_for_wan_loc = dmz_h_eff_loc

            col_right_loc = ix_eff_loc + left_col_w_loc

            dmz_x_loc = col_right_loc + SEGMENT_GAP
            access_top_loc = int(dmz_dy)
            dmz_y_loc = access_top_loc

            if has_dmz_data:
                int_net_x_loc = dmz_x_loc + max(int(dmz_w_eff_loc), int(wan_w_eff_loc)) + SEGMENT_GAP
            else:
                wan_stack_right = col_right_loc
                if int_wan_oids:
                    wan_stack_right = max(
                        wan_stack_right,
                        ix_eff_loc + max(int(segment_size[o]['w']) for o in int_wan_oids),
                    )
                int_net_x_loc = wan_stack_right + SEGMENT_GAP
            int_net_y_loc = access_top_loc

            _, _, inw_net_t, inh_net_t = int_net_tpl
            int_net_w_eff_loc = (
                effective_segment_width(int_net_oid, inw_net_t, wan_layout_cache)
                if int_net_oid
                else inw_net_t
            )

            int_sec_x_loc = int_net_x_loc + int_net_w_eff_loc + SEGMENT_GAP

            int_wan_x_loc = dmz_x_loc
            if not has_dmz_data:
                int_wan_x_loc = ix_eff_loc
                ext_h_loc = int(exh_loc)
                int_wan_y_loc = int(iy + inh_loc + INET_EXT_VERTICAL_GAP + ext_h_loc + SEGMENT_GAP)
            else:
                int_wan_y_loc = dmz_y_loc + dmz_h_for_wan_loc + SEGMENT_GAP

            ox_inet, oy_inet = int(ix_eff_loc), int(iy)
            for oid in inet_oids:
                iw_o = effective_segment_width(oid, iw_t, wan_layout_cache)
                ih_o = effective_segment_height(oid, ih_t, wan_layout_cache)
                segment_origin[oid] = {'x': ox_inet, 'y': oy_inet}
                positions[oid] = {'x': ox_inet, 'y': oy_inet, 'w': int(iw_o), 'h': int(ih_o)}

            oy_ext = int(iy + inh_loc + INET_EXT_VERTICAL_GAP)
            ox_ext = int(ix_eff_loc)
            for oid in ext_oids:
                ew_o = effective_segment_width(oid, ew_t, wan_layout_cache)
                eh_o = effective_segment_height(oid, eh_t, wan_layout_cache)
                segment_origin[oid] = {'x': ox_ext, 'y': oy_ext}
                positions[oid] = {'x': ox_ext, 'y': oy_ext, 'w': int(ew_o), 'h': int(eh_o)}

            for oid in dmz_oids_on_page:
                segment_origin[oid] = {'x': int(dmz_x_loc), 'y': int(dmz_y_loc)}
            if int_net_oid:
                segment_origin[int_net_oid] = {'x': int(int_net_x_loc), 'y': int(int_net_y_loc)}
            if int_sec_oid and int_sec_tpl:
                segment_origin[int_sec_oid] = {'x': int(int_sec_x_loc), 'y': int(access_top_loc)}
            for oid in int_wan_oids:
                ww_o = int(segment_size.get(oid, {}).get('w', wan_w_t))
                wh_o = int(segment_size.get(oid, {}).get('h', wan_h_t))
                segment_origin[oid] = {'x': int(int_wan_x_loc), 'y': int(int_wan_y_loc)}
                positions[oid] = {'x': int(int_wan_x_loc), 'y': int(int_wan_y_loc), 'w': ww_o, 'h': wh_o}

        _refresh_interior_grid()

        xf_acc: frozenset = frozenset(
            apply_cross_segment_firewall_positions(
                positions, segment_origin, components, networks, page_roots, patterns_doc, segment_size,
            ),
        )

        _OVERFLOW_GRID_ITER_MAX = 8
        for _ in range(_OVERFLOW_GRID_ITER_MAX):
            grew = expand_interior_segments_from_child_bbox(
                positions, segment_size, segments, networks, components, page_roots, patterns_doc,
            )
            if not grew:
                break
            _refresh_interior_grid()
            xf_acc |= frozenset(
                apply_cross_segment_firewall_positions(
                    positions, segment_origin, components, networks, page_roots, patterns_doc, segment_size,
                ),
            )

        return {
            'positions': positions,
            'segment_size': segment_size,
            'segment_origin': segment_origin,
            'cross_segment_firewall_oids': xf_acc,
        }

    # ---------- прочие шаблоны (dc и т.д.): только DMZ + сдвиг INT-NET ---
    rights_inet: List[int] = []
    if inet_rect:
        ix2, iy2, iw2, ih2 = inet_rect
        for oid, seg in segments.items():
            if not isinstance(seg, dict):
                continue
            if seg.get('zone') != 'INET-EDGE':
                continue
            if not location_on_page(seg.get('location'), page_roots):
                continue
            ew = effective_segment_width(oid, iw2, wan_layout_cache)
            rights_inet.append(ix2 + ew)

    if rights_inet:
        anchor_x = max(rights_inet) + SEGMENT_GAP
    elif wan_rect:
        anchor_x = int(wan_rect[0])
    else:
        anchor_x = int(dmz_dx)

    if not dmz_oids_on_page:
        return {
            'positions': positions,
            'segment_size': segment_size,
            'segment_origin': segment_origin,
            'cross_segment_firewall_oids': frozenset(),
        }

    dmz_w_eff = int(dmz_dw)
    for oid in dmz_oids_on_page:
        dmz_w_eff = max(dmz_w_eff, int(segment_size.get(oid, {}).get('w', dmz_dw)))

    int_net_rect = segment_rect_for_zone(patterns_doc, 'INT-NET')
    int_net_tpl_x = int(int_net_rect[0]) if int_net_rect else 0
    if int_net_rect:
        int_net_new_x = anchor_x + dmz_w_eff + SEGMENT_GAP
        delta_push = int_net_new_x - int_net_tpl_x
    else:
        delta_push = 0

    for oid in dmz_oids_on_page:
        segment_origin[oid] = {'x': int(anchor_x), 'y': int(dmz_dy)}

    if int_net_rect:
        for oid, seg in segments.items():
            if not isinstance(seg, dict):
                continue
            z = seg.get('zone') or ''
            if z == 'DMZ' or z not in _RIGHT_OF_STACK_ZONES:
                continue
            if not location_on_page(seg.get('location'), page_roots):
                continue
            rect = segment_rect_for_zone(patterns_doc, z)
            if not rect:
                continue
            tpl_x, tpl_y = int(rect[0]), int(rect[1])
            if tpl_x < int_net_tpl_x:
                continue
            segment_origin[oid] = {'x': tpl_x + delta_push, 'y': tpl_y}

    xf = frozenset(
        apply_cross_segment_firewall_positions(
            positions, segment_origin, components, networks, page_roots, patterns_doc, segment_size,
        ),
    )

    return {
        'positions': positions,
        'segment_size': segment_size,
        'segment_origin': segment_origin,
        'cross_segment_firewall_oids': xf,
    }
