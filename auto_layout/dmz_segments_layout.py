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

        inh = (
            max(
                (effective_segment_height(o, ih_t, wan_layout_cache) for o in inet_oids),
                default=ih_t,
            )
            if inet_oids
            else ih_t
        )
        inw = (
            max(
                (effective_segment_width(o, iw_t, wan_layout_cache) for o in inet_oids),
                default=iw_t,
            )
            if inet_oids
            else iw_t
        )

        _, _, ew_t, eh_t = ext_tpl
        exh = (
            max(
                (effective_segment_height(o, eh_t, wan_layout_cache) for o in ext_oids),
                default=eh_t,
            )
            if ext_oids
            else eh_t
        )
        exw = (
            max(
                (effective_segment_width(o, ew_t, wan_layout_cache) for o in ext_oids),
                default=ew_t,
            )
            if ext_oids
            else ew_t
        )

        left_col_w = max(inw, exw)

        _, _, wan_w_t, wan_h_t = wan_edge_tpl
        if int_wan_oids:
            wan_w_natural = max(
                int(segment_size.get(o, {}).get('w', wan_w_t)) for o in int_wan_oids
            )
        else:
            wan_w_natural = int(wan_w_t)

        dmz_w_tpl = int(dmz_dw)
        dmz_h_eff = int(dmz_dh)
        for oid in dmz_oids_on_page:
            dmz_w_tpl = max(dmz_w_tpl, int(segment_size.get(oid, {}).get('w', dmz_dw)))
            dmz_h_eff = max(
                dmz_h_eff,
                effective_segment_height(oid, int(dmz_dh), wan_layout_cache),
            )

        has_dmz_data = bool(dmz_oids_on_page)
        if not has_dmz_data:
            # Нет DMZ в данных: освобождаем ширину колонки DMZ — INET/EXT сдвигаются к INT-NET / ЛВС
            pull_shift = max(0, dmz_w_tpl - wan_w_natural)
            ix_eff = ix + pull_shift
            dmz_w_eff = wan_w_natural
            for _iw in int_wan_oids:
                # Под EXT: ширина полосы = колонка INET/EXT
                segment_size.setdefault(_iw, {})['w'] = int(left_col_w)
            wan_w_eff = wan_w_natural
            dmz_h_for_wan = 0
        else:
            ix_eff = ix
            dmz_w_eff = dmz_w_tpl
            if int_wan_oids:
                for _iw in int_wan_oids:
                    segment_size.setdefault(_iw, {})['w'] = int(dmz_w_eff)
                wan_w_eff = int(dmz_w_eff)
            else:
                wan_w_eff = int(wan_w_t)
            dmz_h_for_wan = dmz_h_eff

        col_right = ix_eff + left_col_w

        dmz_x = col_right + SEGMENT_GAP
        # Как в dc.yaml: верх DMZ / INT-NET / INT-SECURITY по шаблону (y=0), не по верху INET (y=210)
        access_top = int(dmz_dy)
        dmz_y = access_top

        int_net_x = max(
            dmz_x + dmz_w_eff + SEGMENT_GAP,
            dmz_x + int(wan_w_eff) + SEGMENT_GAP,
        )
        if not has_dmz_data:
            # Без колонки DMZ внутренняя сеть (ЦОД / ЛВС) — сразу после полоски INET/EXT
            int_net_x = col_right + SEGMENT_GAP
        int_net_y = access_top

        _, _, inw_net_t, inh_net_t = int_net_tpl
        int_net_w_eff = effective_segment_width(int_net_oid, inw_net_t, wan_layout_cache) if int_net_oid else inw_net_t

        int_sec_x = int_net_x + int_net_w_eff + SEGMENT_GAP

        # INT-WAN: при DMZ — под колонкой DMZ; без DMZ в данных — под EXT-WAN-EDGE, та же X-колонка что INET/EXT
        int_wan_x = dmz_x
        if not has_dmz_data:
            int_wan_x = ix_eff
            ext_h = int(exh)
            int_wan_y = int(iy + inh + INET_EXT_VERTICAL_GAP + ext_h + SEGMENT_GAP)
        else:
            int_wan_y = dmz_y + dmz_h_for_wan + SEGMENT_GAP

        ox_inet, oy_inet = int(ix_eff), int(iy)
        for oid in inet_oids:
            iw_o = effective_segment_width(oid, iw_t, wan_layout_cache)
            ih_o = effective_segment_height(oid, ih_t, wan_layout_cache)
            segment_origin[oid] = {'x': ox_inet, 'y': oy_inet}
            positions[oid] = {'x': ox_inet, 'y': oy_inet, 'w': int(iw_o), 'h': int(ih_o)}

        oy_ext = int(iy + inh + INET_EXT_VERTICAL_GAP)
        ox_ext = int(ix_eff)
        for oid in ext_oids:
            ew_o = effective_segment_width(oid, ew_t, wan_layout_cache)
            eh_o = effective_segment_height(oid, eh_t, wan_layout_cache)
            segment_origin[oid] = {'x': ox_ext, 'y': oy_ext}
            positions[oid] = {'x': ox_ext, 'y': oy_ext, 'w': int(ew_o), 'h': int(eh_o)}

        for oid in dmz_oids_on_page:
            segment_origin[oid] = {'x': int(dmz_x), 'y': int(dmz_y)}
        if int_net_oid:
            segment_origin[int_net_oid] = {'x': int(int_net_x), 'y': int(int_net_y)}
        if int_sec_oid and int_sec_tpl:
            segment_origin[int_sec_oid] = {'x': int(int_sec_x), 'y': int(access_top)}
        for oid in int_wan_oids:
            ww_o = int(segment_size.get(oid, {}).get('w', wan_w_t))
            wh_o = int(segment_size.get(oid, {}).get('h', wan_h_t))
            segment_origin[oid] = {'x': int(int_wan_x), 'y': int(int_wan_y)}
            positions[oid] = {'x': int(int_wan_x), 'y': int(int_wan_y), 'w': ww_o, 'h': wh_o}

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
