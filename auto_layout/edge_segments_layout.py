"""
Раскладка WAN-edge и (для office.yaml / dc.yaml) intrinsic для DMZ, INT-NET, INT-SECURITY-NET и т.д.
Позиции контейнеров зон на странице задаются в dmz_segments_layout (сетка как у office для dc тоже).
"""
from __future__ import annotations

from typing import Any, Dict, List, Set

from auto_layout.layout_pattern_modes import patterns_yaml_uses_interior_layout
from auto_layout.segment_intrinsic_layout import compute_intrinsic_band_layout

WAN_EDGE_ZONES = frozenset({'INT-WAN-EDGE', 'INET-EDGE', 'EXT-WAN-EDGE'})

# На страницах с interior_layout (офис ЦОД, головной офис) считаем содержимое и размеры также для внутренних зон
OFFICE_INTRINSIC_ZONES = WAN_EDGE_ZONES | frozenset({
    'DMZ',
    'INT-NET',
    'INT-SECURITY-NET',
})


def intrinsic_zones_for_pattern(patterns_yaml_path: str) -> Set[str]:
    if patterns_yaml_uses_interior_layout(patterns_yaml_path):
        return set(OFFICE_INTRINSIC_ZONES)
    return set(WAN_EDGE_ZONES)


def edge_segments_layout(
    sd: Any,
    conf: Dict[str, Any],
    page_name: str,
    page_roots: List[str],
    patterns_yaml_path: str,
) -> Dict[str, Any]:
    """
    :return: positions / segment_size / segment_origin (origin для паттернов без interior_layout не задаётся здесь).
    """
    if page_name == 'Main Schema':
        return {'positions': {}, 'segment_size': {}, 'segment_origin': {}}

    zones = frozenset(intrinsic_zones_for_pattern(patterns_yaml_path))
    lay = compute_intrinsic_band_layout(zones, sd, conf, page_roots, patterns_yaml_path)
    return {
        'positions': lay.get('positions') or {},
        'segment_size': lay.get('segment_size') or {},
        'segment_origin': {},
    }


def resolve_page_location_roots(sd: Any, conf: Dict[str, Any], page_title: str, patterns_yaml_path: str) -> List[str]:
    """OID локации страницы по title (как в add_pages), если diagram_ids ещё не заполнен."""
    import os

    if not os.path.isfile(patterns_yaml_path):
        return []
    doc = sd.read_yaml_file(patterns_yaml_path) or {}
    for pat in doc.values():
        if not isinstance(pat, dict) or not pat.get('ext_page'):
            continue
        schema = pat.get('schema')
        if not schema:
            continue
        try:
            pdata = sd.get_object(conf['data_yaml_file'], schema)
        except Exception:
            continue
        for oid, row in pdata.items():
            if isinstance(row, dict) and row.get('title') == page_title:
                return [oid]
    return []
