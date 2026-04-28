"""
Общая раскладка LAN / router / fpsu / vpn / switch слева, firewall справа внутри зоны сегмента.
Используется для WAN-edge и DMZ (и других наборов зон по OID из данных).
"""
from __future__ import annotations

import os
import re
from typing import Any, AbstractSet, Dict, List, Optional, Tuple

from auto_layout.layout_cache import (
    data_sources_fingerprint,
    get_intrinsic_cached,
    set_intrinsic_cached,
)

# Единый зазор между колонками/рядами и от краёв контейнера сегмента (px)
SEGMENT_GAP = 10

# Общий сдвиг блока LAN/устройств внутри сегмента вниз (px)
LAN_BAND_VERTICAL_OFFSET = 60

# Вертикальный зазор между INET-EDGE и EXT-WAN-EDGE на странице office (пиксели)
INET_EXT_VERTICAL_GAP = 40

# Оценка размера подписи LAN (мелкий шрифт в шаблоне drawio): символы × px по длинной стороне «капсулы»
_LAN_CHAR_PX = 6
_LAN_MIN_BAR_LEN = 88  # не короче паттерна по умолчанию (~14 символов)
# Толщина полосы LAN ≈ размер шрифта подписи в шаблоне drawio (~9–10px)
_LAN_BAR_THICKNESS_PX = 11

# Узкий столбец слева от DMZ/WAN: не расширять ширину за шаблон (иначе наезжает на соседние зоны)
NARROW_STRIP_ZONES = frozenset({'INET-EDGE', 'EXT-WAN-EDGE'})


def location_on_page(location: Any, page_roots: List[str]) -> bool:
    if not page_roots:
        return False
    if location is None:
        return False
    candidates = location if isinstance(location, list) else [location]
    return any(c in page_roots for c in candidates)


def segment_rect_for_zone(patterns_doc: Dict[str, Any], zone: str) -> Optional[Tuple[int, int, int, int]]:
    prefix = f'zone:{zone}'
    for pat in patterns_doc.values():
        if not isinstance(pat, dict):
            continue
        if pat.get('schema') != 'seaf.company.ta.services.network_segments':
            continue
        t = pat.get('type') or ''
        if t == prefix:
            return (
                int(pat.get('x', 0)),
                int(pat.get('y', 0)),
                int(pat.get('w', 100)),
                int(pat.get('h', 100)),
            )
    return None


def _match_exclude_any(data: Dict[str, Any], regex_dict: Optional[Dict[str, str]]) -> bool:
    if not regex_dict:
        return False
    for field, rx in regex_dict.items():
        val = data.get(field)
        if val is None:
            continue
        try:
            if re.search(rx, str(val), re.I):
                return True
        except re.error:
            continue
    return False


def _match_any_field(data: Dict[str, Any], regex_dict: Optional[Dict[str, str]]) -> bool:
    if not regex_dict:
        return False
    for field, rx in regex_dict.items():
        val = data.get(field)
        if val is None:
            continue
        try:
            if re.search(rx, str(val), re.I):
                return True
        except re.error:
            continue
    return False


def classify_band_component(data: Dict[str, Any], specs: Dict[str, Dict[str, Any]]) -> Optional[str]:
    """'left' | 'right' | None."""
    typ = data.get('type') or ''
    if typ == 'Маршрутизатор (роутер)':
        vpn_pat = specs.get('vpn') or {}
        if _match_any_field(data, vpn_pat.get('any_field_regex')):
            return 'left'
        router_pat = specs.get('router') or {}
        if _match_exclude_any(data, router_pat.get('exclude_any_field_regex')):
            return None
        return 'left'
    if typ == 'Межсетевой экран (файрвол)':
        fpsu_pat = specs.get('fpsu') or {}
        if _match_any_field(data, fpsu_pat.get('any_field_regex')):
            return 'left'
        fw_pat = specs.get('firewall') or {}
        if _match_exclude_any(data, fw_pat.get('exclude_any_field_regex')):
            return None
        return 'right'
    if typ == 'Коммутатор (свитч)':
        return 'left'
    return None


def _first_lan_oid_for_segment_in_connection_order(
    comp: Dict[str, Any],
    networks: Dict[str, Any],
    segment_oid: str,
) -> Optional[str]:
    """Первая в порядке network_connection сеть с type=LAN и segment=текущему сегменту."""
    raw = comp.get('network_connection')
    if raw is None:
        return None
    conns = raw if isinstance(raw, list) else [raw]
    for nid in conns:
        if not nid:
            continue
        nd = networks.get(nid)
        if not isinstance(nd, dict):
            continue
        if nd.get('type') != 'LAN':
            continue
        seg = nd.get('segment')
        if seg == segment_oid:
            return nid
        if isinstance(seg, list) and segment_oid in seg:
            return nid
    return None


def linked_lan_oid(
    comp: Dict[str, Any],
    networks: Dict[str, Any],
    segment_oid: str,
) -> Optional[str]:
    return _first_lan_oid_for_segment_in_connection_order(comp, networks, segment_oid)


def linked_lan_oid_firewall_column(
    comp: Dict[str, Any],
    networks: Dict[str, Any],
    segment_oid: str,
) -> Optional[str]:
    """Firewall справа от LAN: та же первая LAN в порядке network_connection для этого сегмента."""
    return _first_lan_oid_for_segment_in_connection_order(comp, networks, segment_oid)


def lan_bar_dimensions_for_network(nd: Dict[str, Any], lan_pat: Dict[str, Any]) -> Tuple[int, int]:
    """
    Размеры как в паттерне lan (w, h); в XML потом width=h, height=w.
    ph — длина полосы вдоль текста; pw — толщина ≈ размер шрифта подписи.
    """
    pw = max(_LAN_BAR_THICKNESS_PX, int(lan_pat.get('w', 20)))
    ph = int(lan_pat.get('h', 140))
    title = str(nd.get('title') or '').strip()
    ipn = str(nd.get('ipnetwork') or '').strip()
    longest = max(len(title), len(ipn), 12)
    bar_len = max(_LAN_MIN_BAR_LEN, longest * _LAN_CHAR_PX + 28)
    bar_len = min(bar_len, 900)
    ph = max(ph, bar_len)
    return pw, ph


def _finalize_segment_vertical(
    out_positions: Dict[str, Dict[str, int]],
    oids: List[str],
    template_base_h: int,
    gap: int,
) -> int:
    """
    Верх не ниже gap; при необходимости увеличивает «внутреннюю» высоту над шаблоном;
    центрирует блок по вертикали, если он помещается в развёрнутый прямоугольник.
    Возвращает высоту содержимого с нижним полем (для segment_size.h).
    """
    if not oids:
        return template_base_h
    ymin = min(out_positions[o]['y'] for o in oids)
    ymax = max(out_positions[o]['y'] + out_positions[o]['h'] for o in oids)
    if ymin < gap:
        d = gap - ymin
        for o in oids:
            out_positions[o]['y'] += int(d)
        ymax += int(d)
        ymin = gap

    min_layout_h = ymax + gap
    layout_h = max(template_base_h, min_layout_h)
    inner = layout_h - 2 * gap
    block_h = ymax - ymin
    if block_h <= inner:
        dy = gap + (inner - block_h) // 2 - ymin
        di = int(dy)
        for o in oids:
            out_positions[o]['y'] += di
        ymax = max(out_positions[o]['y'] + out_positions[o]['h'] for o in oids)

    layout_h = max(layout_h, ymax + gap)
    return layout_h


def _center_segment_horizontal_widen(
    out_positions: Dict[str, Dict[str, int]],
    oids: List[str],
    template_base_w: int,
    gap: int,
) -> None:
    """
    Центрирует блок по горизонтали; если шире шаблона — считаем целевую ширину span+2*gap.
    """
    if not oids or template_base_w <= 2 * gap:
        return
    min_x = min(out_positions[o]['x'] for o in oids)
    max_r = max(out_positions[o]['x'] + out_positions[o]['w'] for o in oids)
    span = max_r - min_x
    inner_tpl = max(1, template_base_w - 2 * gap)
    base_w_eff = max(template_base_w, span + 2 * gap) if span > inner_tpl else template_base_w
    inner = base_w_eff - 2 * gap
    dx = gap + (inner - span) // 2 - min_x
    for o in oids:
        out_positions[o]['x'] += int(dx)


WIDE_CENTER_ZONES = frozenset({'INT-NET'})


def compute_intrinsic_band_layout(
    zones: AbstractSet[str],
    sd: Any,
    conf: Dict[str, Any],
    page_roots: List[str],
    patterns_yaml_path: str,
) -> Dict[str, Dict[str, Any]]:
    """
    positions / segment_size для OID в указанных зонах (данные network_segments.zone).
    """
    pat_abs = os.path.abspath(os.path.expanduser(patterns_yaml_path))
    try:
        pst = os.stat(pat_abs)
        pat_sig = (pst.st_mtime_ns, pst.st_size)
    except OSError:
        pat_sig = (0, -1)

    cache_key = (
        data_sources_fingerprint(sd, conf),
        pat_abs,
        pat_sig,
        tuple(sorted(zones)),
        tuple(page_roots),
        SEGMENT_GAP,
        INET_EXT_VERTICAL_GAP,
        _LAN_BAR_THICKNESS_PX,
        'v6-lan-dy-after-finalize',
    )
    cached = get_intrinsic_cached(cache_key)
    if cached is not None:
        return cached

    out_positions: Dict[str, Dict[str, int]] = {}
    out_segment_size: Dict[str, Dict[str, int]] = {}

    def _done() -> Dict[str, Dict[str, Any]]:
        out = {'positions': out_positions, 'segment_size': out_segment_size}
        set_intrinsic_cached(cache_key, out)
        return out

    if not page_roots:
        return _done()

    if not os.path.isfile(patterns_yaml_path):
        return _done()

    patterns_doc = sd.read_yaml_file(patterns_yaml_path) or {}
    try:
        lan_pat = patterns_doc.get('lan') or {}
        router_pat = patterns_doc.get('router') or {}
        firewall_pat = patterns_doc.get('firewall') or {}
        fpsu_pat = patterns_doc.get('fpsu') or {}
        vpn_pat = patterns_doc.get('vpn') or {}
        switch_pat = patterns_doc.get('switch') or {}
    except Exception:
        return _done()

    specs = {
        'router': router_pat,
        'firewall': firewall_pat,
        'fpsu': fpsu_pat,
        'vpn': vpn_pat,
        'switch': switch_pat,
    }

    try:
        merged = sd.read_and_merge_yaml(conf.get('data_yaml_file'))
        segments = merged.get('seaf.company.ta.services.network_segments') or {}
        networks = merged.get('seaf.company.ta.services.networks') or {}
        components = merged.get('seaf.company.ta.components.networks') or {}
    except Exception:
        return _done()

    zone_segments: Dict[str, Dict[str, Any]] = {}
    for seg_oid, seg in segments.items():
        if not isinstance(seg, dict):
            continue
        zone = seg.get('zone') or ''
        if zone not in zones:
            continue
        if not location_on_page(seg.get('location'), page_roots):
            continue
        zone_segments[seg_oid] = seg

    if not zone_segments:
        return _done()

    is_office_yaml = os.path.basename(patterns_yaml_path).lower() == 'office.yaml'

    office_global_pw: Optional[int] = None
    office_global_ph: Optional[int] = None
    if is_office_yaml:
        gpw, gph = 0, 0
        for _soid, _seg in zone_segments.items():
            _zone = _seg.get('zone') or ''
            if _zone not in zones:
                continue
            if not segment_rect_for_zone(patterns_doc, _zone):
                continue
            for nid, nd in networks.items():
                if not isinstance(nd, dict):
                    continue
                if nd.get('type') != 'LAN':
                    continue
                if nd.get('segment') != _soid and not (
                    isinstance(nd.get('segment'), list) and _soid in nd.get('segment', [])
                ):
                    continue
                pw_i, ph_i = lan_bar_dimensions_for_network(nd, lan_pat)
                gpw = max(gpw, pw_i)
                gph = max(gph, ph_i)
        if gpw > 0 and gph > 0:
            office_global_pw, office_global_ph = gpw, gph

    for seg_oid, seg in zone_segments.items():
        zone = seg.get('zone') or ''
        rect = segment_rect_for_zone(patterns_doc, zone)
        if not rect:
            continue
        _sx, _sy, base_w, base_h = rect

        lans_in_seg = [
            nid for nid, nd in networks.items()
            if isinstance(nd, dict)
            and nd.get('type') == 'LAN'
            and (
                nd.get('segment') == seg_oid
                or (isinstance(nd.get('segment'), list) and seg_oid in nd.get('segment', []))
            )
        ]
        lans_in_seg.sort(key=lambda x: (networks.get(x) or {}).get('title') or x)

        left_by_lan: Dict[str, List[str]] = {}
        right_by_lan: Dict[str, List[str]] = {}

        for cid, cdata in components.items():
            if not isinstance(cdata, dict):
                continue
            if cdata.get('segment') != seg_oid:
                continue
            if not location_on_page(cdata.get('location'), page_roots):
                continue
            side = classify_band_component(cdata, specs)
            if side is None:
                continue
            if side == 'right':
                lan_oid = linked_lan_oid_firewall_column(cdata, networks, seg_oid)
            else:
                lan_oid = linked_lan_oid(cdata, networks, seg_oid)
            if not lan_oid:
                continue
            if lan_oid not in lans_in_seg:
                continue
            if side == 'left':
                left_by_lan.setdefault(lan_oid, []).append(cid)
            else:
                right_by_lan.setdefault(lan_oid, []).append(cid)

        max_content_right = SEGMENT_GAP
        oids_this_seg: List[str] = []

        def dims_for(oid: str) -> Tuple[int, int]:
            cdata = components.get(oid) or {}
            typ = cdata.get('type') or ''
            if typ == 'Маршрутизатор (роутер)':
                if _match_any_field(cdata, vpn_pat.get('any_field_regex')):
                    return int(vpn_pat.get('w', 50)), int(vpn_pat.get('h', 20))
                return int(router_pat.get('w', 40)), int(router_pat.get('h', 30))
            if typ == 'Межсетевой экран (файрвол)':
                if _match_any_field(cdata, fpsu_pat.get('any_field_regex')):
                    return int(fpsu_pat.get('w', 30)), int(fpsu_pat.get('h', 30))
                return int(firewall_pat.get('w', 20)), int(firewall_pat.get('h', 40))
            if typ == 'Коммутатор (свитч)':
                return int(switch_pat.get('w', 50)), int(switch_pat.get('h', 20))
            return 40, 30

        if not lans_in_seg and not left_by_lan and not right_by_lan:
            out_segment_size[seg_oid] = {'w': base_w, 'h': base_h}
            continue

        row_specs: List[Dict[str, Any]] = []
        for lan_oid in lans_in_seg:
            nd = networks.get(lan_oid) or {}
            pw, ph = lan_bar_dimensions_for_network(nd, lan_pat)
            left_ids = left_by_lan.get(lan_oid, [])
            right_ids = right_by_lan.get(lan_oid, [])
            left_ids.sort(key=lambda x: ((components.get(x) or {}).get('title') or x))
            right_ids.sort(key=lambda x: ((components.get(x) or {}).get('title') or x))

            left_boxes = [(oid, dims_for(oid)) for oid in left_ids]
            right_boxes = [(oid, dims_for(oid)) for oid in right_ids]

            left_w = max((w for _, (w, _) in left_boxes), default=0)
            right_w = max((w for _, (w, _) in right_boxes), default=0)

            left_stack_h = sum(h for _, (_, h) in left_boxes)
            if len(left_boxes) > 1:
                left_stack_h += SEGMENT_GAP * (len(left_boxes) - 1)
            right_stack_h = sum(h for _, (_, h) in right_boxes)
            if len(right_boxes) > 1:
                right_stack_h += SEGMENT_GAP * (len(right_boxes) - 1)

            row_h = max(ph, left_stack_h, right_stack_h)

            row_specs.append({
                'lan_oid': lan_oid,
                'pw': pw,
                'ph': ph,
                'left_boxes': left_boxes,
                'right_boxes': right_boxes,
                'left_w': left_w,
                'right_w': right_w,
                'left_stack_h': left_stack_h,
                'right_stack_h': right_stack_h,
                'row_h': row_h,
            })

        # Один формат полос LAN на всей странице office (глобальный max pw/ph по всем сегментам)
        if is_office_yaml and row_specs and office_global_pw is not None and office_global_ph is not None:
            u_pw, u_ph = office_global_pw, office_global_ph
            for s in row_specs:
                s['ph'] = u_ph
                s['pw'] = u_pw
                s['row_h'] = max(u_ph, s['left_stack_h'], s['right_stack_h'])

        n_rows = len(row_specs)
        inner_avail_h = max(1, base_h - 2 * SEGMENT_GAP)
        sum_row_h = sum(s['row_h'] for s in row_specs)
        gap_budget = inner_avail_h - sum_row_h
        if n_rows >= 1 and gap_budget >= 0:
            gap_uniform = gap_budget / float(n_rows + 1)
        else:
            gap_uniform = float(SEGMENT_GAP)

        cur_y = SEGMENT_GAP + gap_uniform

        for spec in row_specs:
            lan_oid = spec['lan_oid']
            pw = spec['pw']
            ph = spec['ph']
            left_w = spec['left_w']
            right_w = spec['right_w']
            left_boxes = spec['left_boxes']
            right_boxes = spec['right_boxes']
            left_stack_h = spec['left_stack_h']
            right_stack_h = spec['right_stack_h']
            row_h = spec['row_h']

            x_lan = SEGMENT_GAP + (left_w + SEGMENT_GAP if left_w else 0)
            x_right = x_lan + ph + (SEGMENT_GAP if right_w else 0)

            y_lan = cur_y + (row_h - ph) / 2
            out_positions[lan_oid] = {
                'x': int(x_lan),
                'y': int(y_lan),
                'w': int(pw),
                'h': int(ph),
            }
            oids_this_seg.append(lan_oid)

            y_left0 = cur_y + (row_h - left_stack_h) / 2
            yy = y_left0
            for oid, (w, h) in left_boxes:
                out_positions[oid] = {
                    'x': int(SEGMENT_GAP + max(0, (left_w - w) / 2)),
                    'y': int(yy),
                    'w': int(w),
                    'h': int(h),
                }
                oids_this_seg.append(oid)
                yy += h + SEGMENT_GAP

            y_right0 = cur_y + (row_h - right_stack_h) / 2
            yr = y_right0
            for oid, (w, h) in right_boxes:
                out_positions[oid] = {
                    'x': int(x_right + max(0, (right_w - w) / 2)),
                    'y': int(yr),
                    'w': int(w),
                    'h': int(h),
                }
                oids_this_seg.append(oid)
                yr += h + SEGMENT_GAP

            inner_right = x_right + right_w
            max_content_right = max(max_content_right, inner_right)
            cur_y += row_h + gap_uniform

        content_width = max(0, max_content_right - SEGMENT_GAP)
        inner_avail_w = max(1, base_w - 2 * SEGMENT_GAP)

        if zone in NARROW_STRIP_ZONES and content_width > inner_avail_w and oids_this_seg:
            scale = inner_avail_w / float(content_width)
            for oid in oids_this_seg:
                p = out_positions[oid]
                p['x'] = int(SEGMENT_GAP + (p['x'] - SEGMENT_GAP) * scale)
                p['w'] = max(1, int(p['w'] * scale))
            max_content_right = SEGMENT_GAP + inner_avail_w

        total_inner_h = cur_y
        if oids_this_seg:
            total_inner_h = _finalize_segment_vertical(
                out_positions, oids_this_seg, base_h, SEGMENT_GAP,
            )

        if zone in WIDE_CENTER_ZONES and oids_this_seg:
            _center_segment_horizontal_widen(out_positions, oids_this_seg, base_w, SEGMENT_GAP)
            max_content_right = SEGMENT_GAP + max(
                out_positions[o]['x'] + out_positions[o]['w'] for o in oids_this_seg
            )

        # После финализации/центрирования: иначе _finalize_segment_vertical снова «центрирует» и гасит сдвиг
        if oids_this_seg and LAN_BAND_VERTICAL_OFFSET:
            for oid in oids_this_seg:
                out_positions[oid]['y'] += LAN_BAND_VERTICAL_OFFSET
            ymax_off = max(
                out_positions[o]['y'] + out_positions[o]['h'] for o in oids_this_seg
            )
            total_inner_h = max(total_inner_h, ymax_off + SEGMENT_GAP)

        if zone in NARROW_STRIP_ZONES:
            total_inner_w = base_w
        else:
            total_inner_w = max(base_w, max_content_right + SEGMENT_GAP)

        out_segment_size[seg_oid] = {
            'w': total_inner_w,
            'h': max(base_h, total_inner_h),
        }

    return _done()


def effective_segment_width(segment_oid: str, template_w: int, layout_cache: Optional[Dict[str, Any]]) -> int:
    if not layout_cache:
        return template_w
    sz = layout_cache.get('segment_size', {}).get(segment_oid)
    if sz and 'w' in sz:
        return int(sz['w'])
    return template_w


def effective_segment_height(segment_oid: str, template_h: int, layout_cache: Optional[Dict[str, Any]]) -> int:
    if not layout_cache:
        return template_h
    sz = layout_cache.get('segment_size', {}).get(segment_oid)
    if sz and 'h' in sz:
        return int(sz['h'])
    return template_h
