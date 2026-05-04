"""
Раскладка DMZ и абсолютные координаты сетки зон (office.yaml и dc.yaml — одна и та же модель).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from auto_layout.layout_pattern_modes import patterns_yaml_uses_interior_layout
from auto_layout.segment_intrinsic_layout import (
    INET_EXT_VERTICAL_GAP,
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


def _oid_zone_on_page(
    segments: Dict[str, Any],
    page_roots: List[str],
    zone: str,
) -> Optional[str]:
    for oid, seg in segments.items():
        if not isinstance(seg, dict):
            continue
        if seg.get('zone') != zone:
            continue
        if not location_on_page(seg.get('location'), page_roots):
            continue
        return oid
    return None


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
    if is_interior and inet_rect and ext_tpl and int_net_tpl and int_sec_tpl and wan_edge_tpl:
        ix, iy, iw_t, ih_t = inet_rect
        inet_oid = _oid_zone_on_page(segments, page_roots, 'INET-EDGE')
        ext_oid = _oid_zone_on_page(segments, page_roots, 'EXT-WAN-EDGE')
        int_net_oid = _oid_zone_on_page(segments, page_roots, 'INT-NET')
        int_sec_oid = _oid_zone_on_page(segments, page_roots, 'INT-SECURITY-NET')
        int_wan_oid = _oid_zone_on_page(segments, page_roots, 'INT-WAN-EDGE')

        inh = effective_segment_height(inet_oid, ih_t, wan_layout_cache) if inet_oid else ih_t
        inw = effective_segment_width(inet_oid, iw_t, wan_layout_cache) if inet_oid else iw_t

        _, _, ew_t, eh_t = ext_tpl
        exh = effective_segment_height(ext_oid, eh_t, wan_layout_cache) if ext_oid else eh_t
        exw = effective_segment_width(ext_oid, ew_t, wan_layout_cache) if ext_oid else ew_t

        left_col_w = max(inw, exw)
        col_right = ix + left_col_w

        dmz_x = col_right + SEGMENT_GAP
        # Как в dc.yaml: верх DMZ / INT-NET / INT-SECURITY по шаблону (y=0), не по верху INET (y=210)
        access_top = int(dmz_dy)
        dmz_y = access_top

        dmz_w_eff = int(dmz_dw)
        dmz_h_eff = int(dmz_dh)
        for oid in dmz_oids_on_page:
            dmz_w_eff = max(dmz_w_eff, int(segment_size.get(oid, {}).get('w', dmz_dw)))
            dmz_h_eff = max(dmz_h_eff, effective_segment_height(oid, int(dmz_dh), wan_layout_cache))

        _, _, wan_w_t, _wan_h_t = wan_edge_tpl
        # Одна вертикальная колонка с DMZ: ширина INT-WAN-EDGE = ширине DMZ, чтобы правый край совпадал
        # и горизонтальный зазор до INT-NET был ровно SEGMENT_GAP (как между DMZ и INT-NET).
        if int_wan_oid:
            segment_size.setdefault(int_wan_oid, {})['w'] = int(dmz_w_eff)
            wan_w_eff = int(dmz_w_eff)
        else:
            wan_w_eff = int(wan_w_t)

        int_net_x = max(
            dmz_x + dmz_w_eff + SEGMENT_GAP,
            dmz_x + int(wan_w_eff) + SEGMENT_GAP,
        )
        int_net_y = access_top

        _, _, inw_net_t, inh_net_t = int_net_tpl
        int_net_w_eff = effective_segment_width(int_net_oid, inw_net_t, wan_layout_cache) if int_net_oid else inw_net_t

        int_sec_x = int_net_x + int_net_w_eff + SEGMENT_GAP

        # INT-WAN под сегментом уровня доступа (DMZ), та же колонка по X — как в dc.yaml
        int_wan_x = dmz_x
        int_wan_y = dmz_y + dmz_h_eff + SEGMENT_GAP

        if inet_oid:
            segment_origin[inet_oid] = {'x': int(ix), 'y': int(iy)}
        if ext_oid:
            segment_origin[ext_oid] = {'x': int(ix), 'y': int(iy + inh + INET_EXT_VERTICAL_GAP)}
        for oid in dmz_oids_on_page:
            segment_origin[oid] = {'x': int(dmz_x), 'y': int(dmz_y)}
        if int_net_oid:
            segment_origin[int_net_oid] = {'x': int(int_net_x), 'y': int(int_net_y)}
        if int_sec_oid:
            segment_origin[int_sec_oid] = {'x': int(int_sec_x), 'y': int(access_top)}
        if int_wan_oid:
            segment_origin[int_wan_oid] = {'x': int(int_wan_x), 'y': int(int_wan_y)}

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

    # ---------- прочие шаблоны (dc и т.д.): только DMZ + сдвиг INT-NET ----------
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
