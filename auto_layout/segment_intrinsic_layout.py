"""
Общая раскладка LAN / router / fpsu / vpn / switch: полосы LAN и колонки устройств.
Полоски LAN слева направо — порядок записей в объединённом YAML networks (не по алфавиту title).
При нескольких LAN у одного файрвола: со 2-й связи и далее — отдельные строки, полоски «справа»;
цепочки таких сетей поднимаются вперёд (merge_lans_row_priority).
Сам файрвол — по горизонтали между полосками LAN1 и LAN2, по вертикали в строке 1-й сети в network_connection
(центрирование по полоске первой LAN). Несколько FW в одном зазоре между теми же LAN — отдельные ряды без наложения.
Одна LAN у FW — без изменений.

Если LAN в сегменте больше паттерна lan.deep (dc/office.yaml): вертикальные колонки по (deep + 3) полосок,
верх всех колонок на одном уровне; между колонками горизонтальный зазор lan.offset * 2 после правого края колонки;
между полосками LAN по вертикали в колонке — LAN_COLUMN_VERTICAL_GAP_PX (40 px).

Используется для WAN-edge и DMZ (и других наборов зон по OID из данных).
"""
from __future__ import annotations

import os
import re
from collections import defaultdict
from typing import Any, AbstractSet, Dict, List, Optional, Set, Tuple

from auto_layout.kb_layout import (
    KB_SCHEMA,
    KB_LAYER_PARENT,
    _absolute_bbox_vertex_cell,
    _absolute_xy_of_mx_cell_top_left,
    _canvas_bbox_for_oid,
    _dc_slug_from_location_root,
    _kb_positioning_mx_cell,
    _network_oid_matches_dc_zone,
    _parse_network_connection,
    _pick_anchor_network_oid,
)
from auto_layout.layout_pattern_modes import patterns_yaml_uses_interior_layout
from auto_layout.layout_cache import (
    data_sources_fingerprint,
    get_intrinsic_cached,
    set_intrinsic_cached,
)
from auto_layout.services_ta_layout import (
    TA_LAYER_PARENT,
    TA_SCHEMAS,
    _ANCHOR_TOP_OFFSET_PX as TA_ANCHOR_TOP_OFFSET_PX,
    _GAP_RIGHT as TA_GAP_RIGHT,
    _GRID_GAP_PX as TA_GRID_GAP_PX,
    _PER_ROW as TA_PER_ROW,
    _ta_positioning_mx_cell,
)
import xml.etree.ElementTree as ET

# Зазор между соседними сегментами на странице (сетка office/dmz), не поля внутри сегмента
SEGMENT_GAP = 10

# Вертикальный зазор между полосками LAN в колоночном режиме (len(LAN) > lan.deep)
LAN_COLUMN_VERTICAL_GAP_PX = 40

# Внутренние поля контента внутри прямоугольника зоны network_segments (px)
INT_SEGMENT_GAP_TOP = 60
INT_SEGMENT_GAP_BOTTOM = 30
INT_SEGMENT_GAP_LEFT = 30
INT_SEGMENT_GAP_RIGHT = 30

# Общий сдвиг блока LAN/устройств внутри сегмента вниз (px)
LAN_BAND_VERTICAL_OFFSET = 60

# Вертикальный зазор между INET-EDGE и EXT-WAN-EDGE на странице office (пиксели)
INET_EXT_VERTICAL_GAP = 40

# Оценка размера подписи LAN (мелкий шрифт в шаблоне drawio): символы × px по длинной стороне «капсулы»
_LAN_CHAR_PX = 6
_LAN_MIN_BAR_LEN = 88  # не короче паттерна по умолчанию (~14 символов)
# Толщина полосы LAN ≈ размер шрифта подписи в шаблоне drawio (~9–10px)
_LAN_BAR_THICKNESS_PX = 11

# При нехватке ширины контент не масштабируем — расширяем segment_size.w (все зоны).


def _intrinsic_row_right_edge_px(spec: Dict[str, Any]) -> float:
    """Правый край строки: после колонки «LAN + справа устройства» при стандартной позиции полосы."""
    lw = float(spec['left_w'])
    ph = float(spec['ph'])
    rw = float(spec['right_w'])
    x_lan = float(INT_SEGMENT_GAP_LEFT + (lw + SEGMENT_GAP if lw else 0))
    x_right = x_lan + ph + (SEGMENT_GAP if rw else 0)
    edge = float(x_right + rw)
    tw = int(spec.get('ta_bbox_w') or 0)
    if tw > 0:
        edge = max(edge, float(x_right + rw + TA_GAP_RIGHT + tw))
    return edge


def location_on_page(location: Any, page_roots: List[str]) -> bool:
    if not page_roots:
        return False
    if location is None:
        return False
    candidates = location if isinstance(location, list) else [location]
    return any(c in page_roots for c in candidates)


def _network_oids_on_page_for_intrinsic(networks: Dict[str, Any], page_roots: List[str]) -> Set[str]:
    out: Set[str] = set()
    for nid, nd in networks.items():
        if isinstance(nd, dict) and location_on_page(nd.get('location'), page_roots):
            out.add(str(nid))
    return out


def _pick_anchor_network_like_kb(nc_list: List[str], on_page: Set[str], location_root: Optional[str]) -> Optional[str]:
    slug = _dc_slug_from_location_root(location_root)
    for nid in nc_list:
        if nid not in on_page:
            continue
        if not _network_oid_matches_dc_zone(nid, slug):
            continue
        return nid
    return None


def _ta_service_counts_by_anchor_lan(
    merged: Dict[str, Any],
    networks: Dict[str, Any],
    lans_in_seg: List[str],
    page_roots: List[str],
) -> Dict[str, int]:
    """
    Сколько сервисов ТА (слой 102, см. services_TA_layout) привязывается к каждой LAN как к якорю.

    Не фильтруем по location объекта: на страницу сервисы попадают через network_connection
    (как services_TA_layout), иначе row_h занижается и сетка не помещается напротив LAN.
    """
    on_page = _network_oids_on_page_for_intrinsic(networks, page_roots)
    loc_root = page_roots[0] if len(page_roots) == 1 else None
    counts: Dict[str, int] = defaultdict(int)
    lan_set = set(lans_in_seg)
    for schema in TA_SCHEMAS:
        chunk = merged.get(schema)
        if not isinstance(chunk, dict):
            continue
        for row in chunk.values():
            if not isinstance(row, dict):
                continue
            nc_list = _parse_network_connection(row.get('network_connection'))
            anchor = _pick_anchor_network_like_kb(nc_list, on_page, loc_root)
            if anchor and anchor in lan_set:
                counts[anchor] += 1
    return dict(counts)


def _max_ta_icon_dims_from_patterns(patterns_doc: Dict[str, Any]) -> Tuple[int, int]:
    mw, mh = 40, 40
    for pat in patterns_doc.values():
        if not isinstance(pat, dict):
            continue
        sch = pat.get('schema')
        if sch not in TA_SCHEMAS:
            continue
        if pat.get('parent_id') != 'network_connection':
            continue
        try:
            mw = max(mw, int(pat.get('w', mw)))
            mh = max(mh, int(pat.get('h', mh)))
        except (TypeError, ValueError):
            pass
    return mw, mh


def _bbox_ta_services_grid_px(n: int, mw: int, mh: int) -> Tuple[int, int]:
    """Оценка ширины/высоты сетки как в services_TA_layout (TA_PER_ROW × ряды)."""
    if n <= 0:
        return 0, 0
    rows = (n + TA_PER_ROW - 1) // TA_PER_ROW
    bbox_w = 0
    for r in range(rows):
        k = min(TA_PER_ROW, n - r * TA_PER_ROW)
        rw = int(k * mw + max(0, k - 1) * TA_GRID_GAP_PX)
        bbox_w = max(bbox_w, rw)
    bbox_h = int(rows * mh + max(0, rows - 1) * TA_GRID_GAP_PX)
    return bbox_w, bbox_h


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
    """Первая LAN в порядке network_connection (для связей данных)."""
    return _first_lan_oid_for_segment_in_connection_order(comp, networks, segment_oid)


def linked_lan_oid_firewall_row_for_layout(
    comp: Dict[str, Any],
    networks: Dict[str, Any],
    segment_oid: str,
) -> Optional[str]:
    """
    LAN, в правой колонке которой располагается FW (ровно один LAN у файрвола).
    Если у FW несколько LAN, запись в right_by_lan не делают — объект ставят между LAN1 и LAN2.
    """
    all_lans = all_segment_lan_oids_ordered(comp, networks, segment_oid)
    return all_lans[0] if all_lans else None


def all_segment_lan_oids_ordered(
    comp: Dict[str, Any],
    networks: Dict[str, Any],
    segment_oid: str,
) -> List[str]:
    """Все сети type=LAN из network_connection для текущего сегмента, в порядке списка."""
    raw = comp.get('network_connection')
    if raw is None:
        return []
    out: List[str] = []
    conns = raw if isinstance(raw, list) else [raw]
    for nid in conns:
        if not nid:
            continue
        nd = networks.get(nid)
        if not isinstance(nd, dict) or nd.get('type') != 'LAN':
            continue
        seg = nd.get('segment')
        if seg == segment_oid or (isinstance(seg, list) and segment_oid in seg):
            out.append(nid)
    return out


def primary_network_segment_oid(nd: Dict[str, Any]) -> Optional[str]:
    """Первый OID сегмента для сети (учёт segment как str или list)."""
    if not nd:
        return None
    sg = nd.get('segment')
    if isinstance(sg, list):
        return sg[0] if sg else None
    return sg if sg else None


def all_connection_lan_oids_ordered(
    comp: Dict[str, Any],
    networks: Dict[str, Any],
) -> List[str]:
    """Все сети type=LAN из network_connection в порядке списка (без фильтра по сегменту компонента)."""
    raw = comp.get('network_connection')
    if raw is None:
        return []
    out: List[str] = []
    conns = raw if isinstance(raw, list) else [raw]
    for nid in conns:
        if not nid:
            continue
        nd = networks.get(nid)
        if not isinstance(nd, dict) or nd.get('type') != 'LAN':
            continue
        out.append(nid)
    return out


def merge_lans_row_priority(
    lans_sorted: List[str],
    segment_oid: str,
    components: Dict[str, Dict[str, Any]],
    networks: Dict[str, Any],
    specs: Dict[str, Dict[str, Any]],
    page_roots: List[str],
    fpsu_pat_local: Dict[str, Any],
) -> List[str]:
    """Поднимает связанные через multi-LAN FW сети в порядке network_connection; остальные — после них,
    в порядке списка lans_sorted (для intrinsic — порядок ключей networks в данных YAML)."""
    valid = frozenset(lans_sorted)
    chains: List[List[str]] = []
    for cdata in components.values():
        if not isinstance(cdata, dict) or cdata.get('segment') != segment_oid:
            continue
        if not location_on_page(cdata.get('location'), page_roots):
            continue
        if classify_band_component(cdata, specs) != 'right':
            continue
        if cdata.get('type') != 'Межсетевой экран (файрвол)':
            continue
        if _match_any_field(cdata, fpsu_pat_local.get('any_field_regex') or {}):
            continue
        als = all_connection_lan_oids_ordered(cdata, networks)
        if len(als) >= 2:
            chains.append(als)

    merged: List[str] = []
    for ch in chains:
        for oid in ch:
            if oid in valid and oid not in merged:
                merged.append(oid)
    for oid in lans_sorted:
        if oid not in merged:
            merged.append(oid)
    return merged


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
    gap_top: int,
    gap_bottom: int,
) -> int:
    """
    Верх не ниже gap_top; низ с полем gap_bottom под нижнюю границу шаблона;
    центрирует блок по вертикали между полями, если помещается в развёрнутый прямоугольник.
    Возвращает высоту сегмента (нижняя граница содержимого + gap_bottom).
    """
    if not oids:
        return template_base_h
    ymin = min(out_positions[o]['y'] for o in oids)
    ymax = max(out_positions[o]['y'] + out_positions[o]['h'] for o in oids)
    if ymin < gap_top:
        d = gap_top - ymin
        for o in oids:
            out_positions[o]['y'] += int(d)
        ymax += int(d)
        ymin = gap_top

    min_layout_h = ymax + gap_bottom
    layout_h = max(template_base_h, min_layout_h)
    inner = layout_h - gap_top - gap_bottom
    block_h = ymax - ymin
    if block_h <= inner and inner > 0:
        dy = gap_top + (inner - block_h) // 2 - ymin
        di = int(dy)
        for o in oids:
            out_positions[o]['y'] += di
        ymax = max(out_positions[o]['y'] + out_positions[o]['h'] for o in oids)

    layout_h = max(layout_h, ymax + gap_bottom)
    return layout_h


def _center_segment_horizontal_widen(
    out_positions: Dict[str, Dict[str, int]],
    oids: List[str],
    template_base_w: int,
    gap_left: int,
    gap_right: int,
) -> None:
    """
    Центрирует блок по горизонтали; если шире шаблона — целевая ширина span+gap_left+gap_right.
    """
    if not oids or template_base_w <= gap_left + gap_right:
        return
    min_x = min(out_positions[o]['x'] for o in oids)
    max_r = max(out_positions[o]['x'] + out_positions[o]['w'] for o in oids)
    span = max_r - min_x
    inner_tpl = max(1, template_base_w - gap_left - gap_right)
    base_w_eff = max(template_base_w, span + gap_left + gap_right) if span > inner_tpl else template_base_w
    inner = base_w_eff - gap_left - gap_right
    dx = gap_left + (inner - span) // 2 - min_x
    for o in oids:
        out_positions[o]['x'] += int(dx)


def connection_lans_span_multiple_segments(lan_oids: List[str], networks: Dict[str, Any]) -> bool:
    seen = set()
    for lid in lan_oids:
        nd = networks.get(lid)
        if not isinstance(nd, dict):
            continue
        so = primary_network_segment_oid(nd)
        if so is not None:
            seen.add(so)
    return len(seen) >= 2


# Минимальный вертикальный шаг между пограничными FW, если по одному pri_row_lan они иначе были бы в одной строке (один цикл стека по первой LAN)
CROSS_FW_STACK_GAP = max(14, SEGMENT_GAP + 4)


def apply_cross_segment_firewall_positions(
    positions: Dict[str, Dict[str, int]],
    segment_origin: Dict[str, Dict[str, int]],
    components: Dict[str, Dict[str, Any]],
    networks: Dict[str, Any],
    page_roots: List[str],
    patterns_doc: Dict[str, Any],
    segment_size: Optional[Dict[str, Dict[str, int]]] = None,
) -> Set[str]:
    """
    После intrinsic и segment_origin: файрволы с LAN в разных сегментах.
    Горизонтально — на одном периметре с «левым» интерфейсом: для всех FW с одним pri_row_lan (одна и та же крайняя полоска зоны)
    X задаётся от правого края этой полоски (+ SEGMENT_GAP), а не середина зазора до каждой правой LAN иначе пары вида Inet→Prod и Inet→Test
    уезжают в разный X как у NGFW-02.
    Вертикально — полоска первой LAN в network_connection (центрирование по высоте полоски).
    Несколько пограничных FW от одной и той же первой LAN (разные вторые LAN или дубль пары):
    общий вертикальный стек по pri_row_lan с зазором CROSS_FW_STACK_GAP, чтобы не стояли в одной строке.
    Возвращает OID файрволов, для которых обновлены координаты (для вывода поверх в draw.io).
    """
    firewall_pat = patterns_doc.get('firewall') or {}
    fpsu_pat = patterns_doc.get('fpsu') or {}
    specs = {
        'router': patterns_doc.get('router') or {},
        'firewall': firewall_pat,
        'fpsu': fpsu_pat,
        'vpn': patterns_doc.get('vpn') or {},
        'switch': patterns_doc.get('switch') or {},
    }

    def _dims_fw(cmp: Dict[str, Any]) -> Tuple[int, int]:
        typ = cmp.get('type') or ''
        if typ != 'Межсетевой экран (файрвол)':
            return 20, 40
        if _match_any_field(cmp, fpsu_pat.get('any_field_regex') or {}):
            return int(fpsu_pat.get('w', 30)), int(fpsu_pat.get('h', 30))
        return int(firewall_pat.get('w', 20)), int(firewall_pat.get('h', 40))

    def _global_lan_left_edge(lid: str) -> Optional[float]:
        nd = networks.get(lid)
        if not isinstance(nd, dict):
            return None
        seg = primary_network_segment_oid(nd)
        if not seg or seg not in segment_origin or lid not in positions:
            return None
        ox = float(segment_origin[seg]['x'])
        return ox + float(positions[lid]['x'])

    def _global_lan_right_edge(lid: str) -> Optional[float]:
        nd = networks.get(lid)
        if not isinstance(nd, dict):
            return None
        seg = primary_network_segment_oid(nd)
        if not seg or seg not in segment_origin or lid not in positions:
            return None
        ox = float(segment_origin[seg]['x'])
        pp = positions[lid]
        return ox + float(pp['x']) + float(pp['h'])

    # (group_key) -> список записей для стека
    pending: List[Dict[str, Any]] = []
    repositioned: Set[str] = set()

    for fw_oid, cdata in components.items():
        if not isinstance(cdata, dict):
            continue
        if not location_on_page(cdata.get('location'), page_roots):
            continue
        if cdata.get('type') != 'Межсетевой экран (файрвол)':
            continue
        if _match_any_field(cdata, fpsu_pat.get('any_field_regex') or {}):
            continue
        if classify_band_component(cdata, specs) != 'right':
            continue
        als_all = all_connection_lan_oids_ordered(cdata, networks)
        if len(als_all) < 2 or not connection_lans_span_multiple_segments(als_all, networks):
            continue
        # первая сеть в данных — строка выравнивания по Y (полоска первой LAN)
        pri_row_lan = als_all[0]
        la, lb = als_all[0], als_all[1]
        if la not in positions or lb not in positions:
            continue
        el_a = _global_lan_left_edge(la)
        el_b = _global_lan_left_edge(lb)
        if el_a is None or el_b is None:
            continue
        # географически слева / справа
        if el_a <= el_b:
            left_lan, right_lan = la, lb
        else:
            left_lan, right_lan = lb, la

        nd_row = networks.get(pri_row_lan)
        if not isinstance(nd_row, dict):
            continue
        seg_row = primary_network_segment_oid(nd_row)
        if not seg_row or seg_row not in segment_origin or pri_row_lan not in positions:
            continue

        sg_fw = cdata.get('segment')
        if sg_fw not in segment_origin:
            continue

        x_r_left = _global_lan_right_edge(left_lan)
        x_l_right = _global_lan_left_edge(right_lan)
        if x_r_left is None or x_l_right is None:
            continue

        fw_w, fh = _dims_fw(cdata)

        oy_s = float(segment_origin[seg_row]['y'])
        ps = positions[pri_row_lan]
        # У полоски LAN: w — вертикальная толщина (ряд по Y), h — горизонтальный размер по X между правым/левым краем
        cy_top_global = oy_s + float(ps['y']) + (float(ps['w']) - float(fh)) / 2.0

        pending.append({
            'fw_oid': fw_oid,
            'cdata': cdata,
            'col': (left_lan, right_lan),
            'pri_row_lan': pri_row_lan,
            'cy_top_global': cy_top_global,
            'fw_w': fw_w,
            'fh': fh,
            'sg_fw': sg_fw,
        })

    # Вертикальный стек по первой LAN: иначе пара FW (одинаковая «левая» LAN, разные правые) даёт одну Y в разных колонках
    buckets: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for p in pending:
        buckets[p['pri_row_lan']].append(p)

    for _pri, plist in buckets.items():
        plist.sort(key=lambda z: (z['cy_top_global'], z['fw_oid']))
        ll0 = plist[0]['col'][0]
        same_geo_left = all(it['col'][0] == ll0 for it in plist)
        x_ge_left_strip = _global_lan_right_edge(ll0)
        prev_bottom: Optional[float] = None
        for it in plist:
            cy_top = float(it['cy_top_global'])
            if prev_bottom is not None:
                min_top = prev_bottom + float(CROSS_FW_STACK_GAP)
                cy_top = max(cy_top, min_top)
            fw_w_i, fh_i = it['fw_w'], it['fh']
            if same_geo_left and x_ge_left_strip is not None:
                # Левый край иконки сразу за правым краем общей «левой» полоски (периметр), не середина до правой LAN
                cx = float(x_ge_left_strip) + float(SEGMENT_GAP)
            else:
                ll, rl = it['col']
                x_r_left = _global_lan_right_edge(ll)
                x_l_right = _global_lan_left_edge(rl)
                if x_r_left is not None and x_l_right is not None and x_l_right > x_r_left + 1:
                    cx = (x_r_left + x_l_right) / 2.0 - float(fw_w_i) / 2.0
                else:
                    cx = (x_r_left or 0) + float(SEGMENT_GAP)

            prev_bottom = cy_top + float(fh_i)
            sg_fw = it['sg_fw']
            o_fw = segment_origin[sg_fw]
            positions[it['fw_oid']] = {
                'x': int(cx - float(o_fw['x'])),
                'y': int(cy_top - float(o_fw['y'])),
                'w': int(fw_w_i),
                'h': int(fh_i),
            }
            repositioned.add(it['fw_oid'])

    if segment_size and pending:
        for p in pending:
            oid = p['fw_oid']
            if oid not in positions:
                continue
            sg = p['sg_fw']
            if not sg:
                continue
            sz = segment_size.setdefault(sg, {})
            pos = positions[oid]
            need_h = int(pos['y']) + int(pos['h']) + int(INT_SEGMENT_GAP_BOTTOM)
            sz['h'] = max(int(sz.get('h', 0)), need_h)

    return repositioned


WIDE_CENTER_ZONES = frozenset({'INT-NET'})

# Если нижний край INT-NET / INT-SECURITY выше низа INT-WAN-EDGE (сегмент «короче» по вертикали) —
# расширяем h до совпадения с низом WAN. Если intrinsic уже опустил низ ниже WAN — не трогаем.
_ALIGN_BOTTOM_REFERENCE_ZONE = 'INT-WAN-EDGE'
_ALIGN_BOTTOM_ADJUST_ZONES = frozenset({'INT-NET', 'INT-SECURITY-NET'})


def _effective_segment_y_and_height(
    seg_oid: str,
    zone: str,
    patterns_doc: Dict[str, Any],
    segment_size: Dict[str, Dict[str, int]],
    segment_origin: Dict[str, Dict[str, int]],
) -> Optional[Tuple[int, int]]:
    """Как в add_object: y из segment_origin при наличии, иначе из паттерна; h из segment_size при наличии, иначе шаблон."""
    rect = segment_rect_for_zone(patterns_doc, zone)
    if not rect:
        return None
    _sx, sy, _sw, sh_tpl = rect
    org = segment_origin.get(seg_oid) or {}
    y_eff = int(org['y']) if 'y' in org else int(sy)
    sz = segment_size.get(seg_oid) or {}
    h_eff = int(sz['h']) if 'h' in sz else int(sh_tpl)
    return y_eff, h_eff


def align_int_net_security_bottom_to_int_wan_edge(
    sd: Any,
    conf: Dict[str, Any],
    page_roots: List[str],
    patterns_yaml_path: str,
    segment_size: Dict[str, Dict[str, int]],
    segment_origin: Optional[Dict[str, Dict[str, int]]] = None,
) -> None:
    """
    После intrinsic + dmz: если низ INT-NET / INT-SECURITY выше низа INT-WAN-EDGE (ось Y вниз),
    увеличиваем h, чтобы совпасть с низом WAN. Если текущий низ уже не ниже WAN — высоту не уменьшаем.

    Учитывает segment_origin и segment_size так же, как отрисовка (иначе эталон WAN считается неверно).
    """
    segment_origin = segment_origin or {}
    if not page_roots or not patterns_yaml_path:
        return
    pat_abs = os.path.abspath(os.path.expanduser(patterns_yaml_path))
    if not os.path.isfile(pat_abs):
        return

    patterns_doc = sd.read_yaml_file(pat_abs) or {}
    if not segment_rect_for_zone(patterns_doc, _ALIGN_BOTTOM_REFERENCE_ZONE):
        return

    try:
        merged = sd.read_and_merge_yaml(conf.get('data_yaml_file'))
        segments = merged.get('seaf.company.ta.services.network_segments') or {}
    except Exception:
        return

    def loc_norm(loc: Any) -> Optional[Tuple[Any, ...]]:
        if loc is None:
            return None
        if isinstance(loc, list):
            return tuple(sorted(loc))
        return (loc,)

    wan_bottom_by_loc: Dict[Tuple[Any, ...], int] = {}
    for seg_oid, seg in segments.items():
        if not isinstance(seg, dict):
            continue
        if seg.get('zone') != _ALIGN_BOTTOM_REFERENCE_ZONE:
            continue
        if not location_on_page(seg.get('location'), page_roots):
            continue
        lk = loc_norm(seg.get('location'))
        if lk is None:
            continue
        yh = _effective_segment_y_and_height(
            seg_oid, _ALIGN_BOTTOM_REFERENCE_ZONE, patterns_doc, segment_size, segment_origin,
        )
        if yh is None:
            continue
        wy_wan, eff_hw = yh
        bot = wy_wan + eff_hw
        wan_bottom_by_loc[lk] = max(wan_bottom_by_loc.get(lk, 0), bot)

    if not wan_bottom_by_loc:
        return

    for seg_oid, seg in segments.items():
        if not isinstance(seg, dict):
            continue
        zone = seg.get('zone') or ''
        if zone not in _ALIGN_BOTTOM_ADJUST_ZONES:
            continue
        if not location_on_page(seg.get('location'), page_roots):
            continue
        lk = loc_norm(seg.get('location'))
        if lk is None or lk not in wan_bottom_by_loc:
            continue
        wan_bot = wan_bottom_by_loc[lk]
        rect = segment_rect_for_zone(patterns_doc, zone)
        if not rect:
            continue
        _sx, sy_tpl, sw, sh_tpl = rect
        org = segment_origin.get(seg_oid) or {}
        sy = int(org['y']) if 'y' in org else int(sy_tpl)
        sz = segment_size.get(seg_oid)
        eff_h = int(sz['h']) if sz and 'h' in sz else int(sh_tpl)
        bot = sy + eff_h
        if bot >= wan_bot:
            continue
        target_h = int(wan_bot - sy)
        if target_h < 1:
            continue
        if sz is None:
            segment_size[seg_oid] = {'w': int(sw), 'h': target_h}
        else:
            if 'w' not in sz:
                sz['w'] = int(sw)
            sz['h'] = target_h


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
        INT_SEGMENT_GAP_TOP,
        INT_SEGMENT_GAP_BOTTOM,
        INT_SEGMENT_GAP_LEFT,
        INT_SEGMENT_GAP_RIGHT,
        INET_EXT_VERTICAL_GAP,
        _LAN_BAR_THICKNESS_PX,
        LAN_COLUMN_VERTICAL_GAP_PX,
        'v28-lan-col-vgap40',
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

    use_uniform_lan_bars = patterns_yaml_uses_interior_layout(patterns_yaml_path)

    uniform_lan_pw: Optional[int] = None
    uniform_lan_ph: Optional[int] = None
    if use_uniform_lan_bars:
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
            uniform_lan_pw, uniform_lan_ph = gpw, gph

    for seg_oid, seg in zone_segments.items():
        zone = seg.get('zone') or ''
        rect = segment_rect_for_zone(patterns_doc, zone)
        if not rect:
            continue
        _sx, _sy, base_w, base_h = rect

        # Порядок полосок слева направо — как в объединённом файле данных (ключи networks по порядку YAML).
        lans_in_seg = [
            nid for nid, nd in networks.items()
            if isinstance(nd, dict)
            and nd.get('type') == 'LAN'
            and (
                nd.get('segment') == seg_oid
                or (isinstance(nd.get('segment'), list) and seg_oid in nd.get('segment', []))
            )
        ]
        lans_ordered = merge_lans_row_priority(
            list(lans_in_seg), seg_oid, components, networks, specs, page_roots, fpsu_pat,
        )

        left_by_lan: Dict[str, List[str]] = {}
        right_by_lan: Dict[str, List[str]] = {}
        multi_fw_between: List[Tuple[str, str, str]] = []

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
                als_here = all_segment_lan_oids_ordered(cdata, networks, seg_oid)
                als_all = all_connection_lan_oids_ordered(cdata, networks)
                not_fpsu = (
                    (cdata.get('type') == 'Межсетевой экран (файрвол)')
                    and not _match_any_field(cdata, fpsu_pat.get('any_field_regex') or {})
                )
                cross_seg_fw = (
                    not_fpsu
                    and len(als_all) >= 2
                    and connection_lans_span_multiple_segments(als_all, networks)
                )
                interior_fw_multi = not_fpsu and len(als_here) >= 2 and not cross_seg_fw
                if interior_fw_multi:
                    multi_fw_between.append((cid, als_here[0], als_here[1]))
                    continue
                if cross_seg_fw:
                    continue
                lan_oid = linked_lan_oid_firewall_row_for_layout(cdata, networks, seg_oid)
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

        # Несколько LAN у FW: 2-я и далее — полоска справа (якорь: 2-я → строка 1; 3-я+ → строка 2)
        fw_strip_anchor_lan: Dict[str, str] = {}
        for cid, cdata in components.items():
            if not isinstance(cdata, dict):
                continue
            if cdata.get('segment') != seg_oid:
                continue
            if not location_on_page(cdata.get('location'), page_roots):
                continue
            if classify_band_component(cdata, specs) != 'right':
                continue
            if cdata.get('type') != 'Межсетевой экран (файрвол)':
                continue
            if _match_any_field(cdata, fpsu_pat.get('any_field_regex') or {}):
                continue
            als_all = all_connection_lan_oids_ordered(cdata, networks)
            if len(als_all) < 2:
                continue
            sec0 = als_all[1]
            for k, ex in enumerate(als_all[1:], start=1):
                if ex not in lans_in_seg:
                    continue
                fw_strip_anchor_lan[ex] = als_all[0] if k == 1 else sec0

        max_content_right = INT_SEGMENT_GAP_LEFT
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
        for lan_oid in lans_ordered:
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
                'fw_strip_anchor_lan': fw_strip_anchor_lan.get(lan_oid),
            })

        ta_counts = _ta_service_counts_by_anchor_lan(merged, networks, lans_ordered, page_roots)
        ta_mw, ta_mh = _max_ta_icon_dims_from_patterns(patterns_doc)
        for spec in row_specs:
            tn = ta_counts.get(spec['lan_oid'], 0)
            tbw, tbh = _bbox_ta_services_grid_px(tn, ta_mw, ta_mh)
            spec['ta_bbox_w'] = tbw
            spec['ta_bbox_h'] = tbh
            spec['ta_row_h_floor'] = int(tbh) if tn > 0 else 0
            if spec['ta_row_h_floor']:
                spec['row_h'] = max(int(spec['row_h']), spec['ta_row_h_floor'])

        # Один формат полос LAN на странице (office / dc: общий max pw/ph по сегментам)
        if use_uniform_lan_bars and row_specs and uniform_lan_pw is not None and uniform_lan_ph is not None:
            u_pw, u_ph = uniform_lan_pw, uniform_lan_ph
            for s in row_specs:
                s['ph'] = u_ph
                s['pw'] = u_pw
                s['row_h'] = max(u_ph, s['left_stack_h'], s['right_stack_h'], int(s.get('ta_row_h_floor', 0)))

        spec_by_lan: Dict[str, Dict[str, Any]] = {s['lan_oid']: s for s in row_specs}

        try:
            lan_deep_thr = int(lan_pat.get('deep', 4) or 4)
        except (TypeError, ValueError):
            lan_deep_thr = 4
        try:
            lan_off_px = int(lan_pat.get('offset', 100) or 100)
        except (TypeError, ValueError):
            lan_off_px = 100
        max_lans_group = max(lan_deep_thr + 3, 1)
        column_horizontal_gap = float(lan_off_px * 2)
        use_lan_vertical_columns = len(row_specs) > lan_deep_thr

        inner_avail_h = max(1, base_h - INT_SEGMENT_GAP_TOP - INT_SEGMENT_GAP_BOTTOM)

        row_y_start: Dict[str, float] = {}
        horizontal_band_h: Optional[float] = None

        if use_lan_vertical_columns:
            chunks = [
                row_specs[i : i + max_lans_group]
                for i in range(0, len(row_specs), max_lans_group)
            ]
            col_heights: List[float] = []
            for chunk in chunks:
                ch = sum(float(s['row_h']) for s in chunk)
                if len(chunk) > 1:
                    ch += LAN_COLUMN_VERTICAL_GAP_PX * (len(chunk) - 1)
                col_heights.append(ch)
            max_stack_h = max(col_heights) if col_heights else 0.0

            gap_budget = inner_avail_h - max_stack_h
            if gap_budget >= 0:
                gap_top = gap_budget / 2.0
            else:
                gap_top = float(SEGMENT_GAP)

            cur_y_top = float(INT_SEGMENT_GAP_TOP) + gap_top
            temp_lan_right_edge: Dict[str, float] = {}
            col_right_max = float(INT_SEGMENT_GAP_LEFT)

            for col_idx, chunk in enumerate(chunks):
                if col_idx > 0:
                    x_column_left = col_right_max + column_horizontal_gap
                else:
                    x_column_left = float(INT_SEGMENT_GAP_LEFT)

                cur_y_row = cur_y_top
                col_right_max_for_chunk = x_column_left

                for spec in chunk:
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

                    x_lan_base = x_column_left + (left_w + SEGMENT_GAP if left_w else 0)
                    anchor_row = spec.get('fw_strip_anchor_lan')
                    if anchor_row and anchor_row in temp_lan_right_edge:
                        x_strip = temp_lan_right_edge[anchor_row] + SEGMENT_GAP
                        x_lan = max(x_lan_base, x_strip)
                    elif anchor_row and anchor_row in spec_by_lan:
                        x_strip = _intrinsic_row_right_edge_px(spec_by_lan[anchor_row]) + SEGMENT_GAP
                        x_lan = max(x_lan_base, x_strip)
                    else:
                        x_lan = x_lan_base
                    x_right = x_lan + ph + (SEGMENT_GAP if right_w else 0)

                    row_y_start[lan_oid] = cur_y_row
                    y_lan = cur_y_row + (row_h - ph) / 2.0
                    out_positions[lan_oid] = {
                        'x': int(x_lan),
                        'y': int(y_lan),
                        'w': int(pw),
                        'h': int(ph),
                    }
                    oids_this_seg.append(lan_oid)

                    y_left0 = cur_y_row + (row_h - left_stack_h) / 2.0
                    yy = y_left0
                    for oid, (w, h) in left_boxes:
                        out_positions[oid] = {
                            'x': int(x_column_left + max(0, (left_w - w) / 2)),
                            'y': int(yy),
                            'w': int(w),
                            'h': int(h),
                        }
                        oids_this_seg.append(oid)
                        yy += h + SEGMENT_GAP

                    y_right0 = cur_y_row + (row_h - right_stack_h) / 2.0
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

                    row_right_edge = x_right + right_w
                    tw = int(spec.get('ta_bbox_w') or 0)
                    if tw > 0:
                        row_right_edge = x_right + right_w + TA_GAP_RIGHT + tw
                    max_content_right = max(max_content_right, row_right_edge)
                    col_right_max_for_chunk = max(col_right_max_for_chunk, row_right_edge)
                    temp_lan_right_edge[lan_oid] = float(x_lan) + float(ph)

                    cur_y_row += row_h + LAN_COLUMN_VERTICAL_GAP_PX

                col_right_max = col_right_max_for_chunk

            cur_y = cur_y_top + max_stack_h + gap_top
        else:
            n_rows = len(row_specs)
            sum_row_h = sum(s['row_h'] for s in row_specs)
            gap_budget = inner_avail_h - sum_row_h
            if n_rows >= 1 and gap_budget >= 0:
                gap_uniform = gap_budget / float(n_rows + 1)
            else:
                gap_uniform = float(SEGMENT_GAP)

            cur_y_f = float(INT_SEGMENT_GAP_TOP) + gap_uniform

            for spec in row_specs:
                lan_oid = spec['lan_oid']
                row_y_start[lan_oid] = cur_y_f
                pw = spec['pw']
                ph = spec['ph']
                left_w = spec['left_w']
                right_w = spec['right_w']
                left_boxes = spec['left_boxes']
                right_boxes = spec['right_boxes']
                left_stack_h = spec['left_stack_h']
                right_stack_h = spec['right_stack_h']
                row_h = spec['row_h']

                x_lan_base = INT_SEGMENT_GAP_LEFT + (left_w + SEGMENT_GAP if left_w else 0)
                anchor_row = spec.get('fw_strip_anchor_lan')
                if anchor_row and anchor_row in spec_by_lan:
                    x_strip = _intrinsic_row_right_edge_px(spec_by_lan[anchor_row]) + SEGMENT_GAP
                    x_lan = max(x_lan_base, x_strip)
                else:
                    x_lan = x_lan_base
                x_right = x_lan + ph + (SEGMENT_GAP if right_w else 0)

                y_lan = cur_y_f + (row_h - ph) / 2.0
                out_positions[lan_oid] = {
                    'x': int(x_lan),
                    'y': int(y_lan),
                    'w': int(pw),
                    'h': int(ph),
                }
                oids_this_seg.append(lan_oid)

                y_left0 = cur_y_f + (row_h - left_stack_h) / 2.0
                yy = y_left0
                for oid, (w, h) in left_boxes:
                    out_positions[oid] = {
                        'x': int(INT_SEGMENT_GAP_LEFT + max(0, (left_w - w) / 2)),
                        'y': int(yy),
                        'w': int(w),
                        'h': int(h),
                    }
                    oids_this_seg.append(oid)
                    yy += h + SEGMENT_GAP

                y_right0 = cur_y_f + (row_h - right_stack_h) / 2.0
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

                row_right_edge = x_right + right_w
                tw = int(spec.get('ta_bbox_w') or 0)
                if tw > 0:
                    row_right_edge = x_right + right_w + TA_GAP_RIGHT + tw
                max_content_right = max(max_content_right, row_right_edge)

                cur_y_f += row_h + gap_uniform

            cur_y = cur_y_f

        # Multi-LAN файрвол: по X между полосками LAN1 и LAN2; по Y — в строке второй LAN (ряд со 2-й сетью)
        for fw_oid, prim, sec in multi_fw_between:
            if prim not in out_positions or sec not in out_positions:
                continue
            if prim not in row_y_start or sec not in row_y_start:
                continue
            ss = spec_by_lan.get(sec)
            if ss is None:
                continue
            fw_w, fh = dims_for(fw_oid)
            x_right_lan1 = float(out_positions[prim]['x']) + float(out_positions[prim]['h'])
            x_left_lan2 = float(out_positions[sec]['x'])
            if x_left_lan2 > x_right_lan1 + 1:
                cx = (x_right_lan1 + x_left_lan2) / 2.0 - float(fw_w) / 2.0
            else:
                cx = x_right_lan1 + float(SEGMENT_GAP)
            rh_sec = float(horizontal_band_h if horizontal_band_h is not None else ss['row_h'])
            cy = float(row_y_start[sec]) + (rh_sec - float(fh)) / 2.0
            out_positions[fw_oid] = {
                'x': int(cx),
                'y': int(cy),
                'w': int(fw_w),
                'h': int(fh),
            }
            oids_this_seg.append(fw_oid)
            max_content_right = max(max_content_right, float(cx + fw_w))

        # Нехватка места по ширине: расширяем сегмент (total_inner_w ниже), без уменьшения w/x scale

        total_inner_h = cur_y
        if oids_this_seg:
            total_inner_h = _finalize_segment_vertical(
                out_positions, oids_this_seg, base_h,
                INT_SEGMENT_GAP_TOP, INT_SEGMENT_GAP_BOTTOM,
            )

        # Горизонтальное центрирование INT-NET перенесено на post-pass
        # center_int_net_content_by_real_bbox (после сервисов ТА/КБ и lan_kant):
        # иначе узкий столбик LAN (~толщина полоски) центрируется в широком сегменте
        # и сети «висят» посередине оранжевой зоны вдали от левого края.

        # После финализации: иначе _finalize_segment_vertical снова «центрирует» и гасит сдвиг
        if oids_this_seg and LAN_BAND_VERTICAL_OFFSET:
            for oid in oids_this_seg:
                out_positions[oid]['y'] += LAN_BAND_VERTICAL_OFFSET
            ymax_off = max(
                out_positions[o]['y'] + out_positions[o]['h'] for o in oids_this_seg
            )
            total_inner_h = max(total_inner_h, ymax_off + INT_SEGMENT_GAP_BOTTOM)

        total_inner_w = max(base_w, max_content_right + INT_SEGMENT_GAP_RIGHT)

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


def _translate_geom_x(mx_cell: ET.Element, dx: float) -> None:
    geom = mx_cell.find('mxGeometry')
    if geom is None or abs(dx) < 0.5:
        return
    try:
        x = float(geom.get('x') or 0)
    except (TypeError, ValueError):
        return
    geom.set('x', str(int(round(x + dx))))


def center_int_net_content_by_real_bbox(
    diagram: Any,
    sd: Any,
    conf: Dict[str, Any],
    page_name: str,
    diagram_ids_map: Dict[str, Any],
    patterns_yaml_path: str,
    location_roots: List[str],
) -> None:
    """
    Горизонтальное выравнивание содержимого INT-NET после сервисов и lan_kant.

    Ранний WIDE_CENTER в intrinsic центрировал только толщину полоски LAN (~20 px) —
    сети уезжали в середину сегмента. Здесь сдвигаем блок (LAN, устройства, lan_kant,
    сервисы КБ/ТА) так, чтобы левый край geom полосок LAN был на INT_SEGMENT_GAP_LEFT —
    как в INT-SECURITY-NET (визуально с rotation=270 это отступ ~120 px от границы сегмента,
    а не 30 px по visual bbox).
    """
    if not patterns_yaml_path or not patterns_yaml_uses_interior_layout(patterns_yaml_path):
        return

    diagram.go_to_diagram(diagram_name=page_name)
    root = diagram.current_root
    page_ids: Set[str] = set(diagram_ids_map.get(page_name) or [])
    location_root = location_roots[0] if len(location_roots) == 1 else None

    try:
        segments = sd.get_object(conf['data_yaml_file'], 'seaf.company.ta.services.network_segments')
        networks = sd.get_object(conf['data_yaml_file'], 'seaf.company.ta.services.networks')
    except Exception:
        return
    if not isinstance(segments, dict) or not isinstance(networks, dict):
        return

    int_net_oids = [
        str(soid)
        for soid, srow in segments.items()
        if isinstance(srow, dict)
        and soid in page_ids
        and location_on_page(srow.get('location'), location_roots)
        and str(srow.get('zone') or '') in WIDE_CENTER_ZONES
    ]
    if not int_net_oids:
        return

    service_rows: Dict[str, Dict[str, Any]] = {}
    for schema in (KB_SCHEMA,) + TA_SCHEMAS:
        try:
            chunk = sd.get_object(conf['data_yaml_file'], schema)
        except Exception:
            continue
        if not isinstance(chunk, dict):
            continue
        for oid, row in chunk.items():
            if oid in page_ids and isinstance(row, dict):
                service_rows.setdefault(str(oid), row)

    for seg_oid in int_net_oids:
        swim = None
        for el in root:
            if el.tag == 'object' and el.get('id') == seg_oid:
                for mx in el.iter('mxCell'):
                    if mx.get('vertex') == '1' and mx.get('parent') == '001':
                        swim = mx
                        break
            elif el.tag == 'mxCell' and el.get('id') == seg_oid and el.get('parent') == '001':
                swim = el
            if swim is not None:
                break
        if swim is None:
            continue
        sgeom = swim.find('mxGeometry')
        if sgeom is None:
            continue
        try:
            seg_w = float(sgeom.get('width') or 0)
        except (TypeError, ValueError):
            continue
        if seg_w <= INT_SEGMENT_GAP_LEFT + INT_SEGMENT_GAP_RIGHT:
            continue

        seg_abs_x, _seg_abs_y = _absolute_xy_of_mx_cell_top_left(root, swim)

        lans_in_seg = {
            nid
            for nid, nd in networks.items()
            if isinstance(nd, dict)
            and nd.get('type') == 'LAN'
            and nid in page_ids
            and (
                nd.get('segment') == seg_oid
                or (isinstance(nd.get('segment'), list) and seg_oid in (nd.get('segment') or []))
            )
        }
        if not lans_in_seg:
            continue

        content_cells: List[ET.Element] = []
        for el in root:
            cells: List[ET.Element] = []
            if el.tag == 'object':
                cells.extend(
                    mx for mx in el.iter('mxCell')
                    if mx.get('vertex') == '1' and mx.get('edge') != '1'
                )
            elif el.tag == 'mxCell' and el.get('vertex') == '1' and el.get('edge') != '1':
                cells.append(el)
            for mx in cells:
                if mx.get('parent') == seg_oid:
                    content_cells.append(mx)

        lan_geom_min_x = float('inf')
        for lan_oid in lans_in_seg:
            obj = root.find(f".//object[@id='{lan_oid}']")
            candidates: List[ET.Element] = []
            if obj is not None:
                candidates.extend(
                    mx for mx in obj.iter('mxCell')
                    if mx.get('vertex') == '1' and mx.get('edge') != '1'
                )
            for mid in (lan_oid, f'{lan_oid}_0'):
                mx = root.find(f".//mxCell[@id='{mid}']")
                if mx is not None:
                    candidates.append(mx)
            for mx in candidates:
                if mx.get('parent') != seg_oid:
                    continue
                ax, _ay = _absolute_xy_of_mx_cell_top_left(root, mx)
                lan_geom_min_x = min(lan_geom_min_x, ax)

        layer_cells: List[ET.Element] = []
        for service_oid, row in service_rows.items():
            nc_list = _parse_network_connection(row.get('network_connection'))
            anchor = _pick_anchor_network_oid(nc_list, page_ids, location_root)
            if not anchor or anchor not in lans_in_seg:
                continue
            pos_mx = _kb_positioning_mx_cell(root, service_oid)
            if pos_mx is None:
                pos_mx = _ta_positioning_mx_cell(root, service_oid)
            if pos_mx is None:
                continue
            layer_cells.append(pos_mx)

        if lan_geom_min_x == float('inf') or not content_cells:
            continue

        # Как INT-SECURITY-NET: левый край geom полоски LAN = INT_SEGMENT_GAP_LEFT.
        target_left = seg_abs_x + float(INT_SEGMENT_GAP_LEFT)
        dx = target_left - lan_geom_min_x
        if abs(dx) < 0.5:
            continue

        for mx in content_cells:
            _translate_geom_x(mx, dx)
        for mx in layer_cells:
            _translate_geom_x(mx, dx)
