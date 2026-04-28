"""Кэш результатов compute_intrinsic_band_layout по данным и паттернам (ускорение повторных прогонов)."""
from __future__ import annotations

import os
from collections import OrderedDict
from copy import deepcopy
from typing import Any, Dict, Optional, Tuple

_MAX_ENTRIES = 128

_intrinsic_cache: "OrderedDict[Tuple, Dict[str, Any]]" = OrderedDict()


def data_sources_fingerprint(sd: Any, conf: Dict[str, Any]) -> Tuple:
    """Отпечаток по mtime+size входных YAML (без полного парсинга при каждом запросе кэша)."""
    paths = sd.expand_data_yaml_sources(conf.get('data_yaml_file'))
    parts = []
    for p in sorted(paths):
        ap = os.path.abspath(os.path.expanduser(p))
        try:
            st = os.stat(ap)
            parts.append((ap, st.st_mtime_ns, st.st_size))
        except OSError:
            parts.append((ap, 0, -1))
    return tuple(parts)


def get_intrinsic_cached(key: Tuple) -> Optional[Dict[str, Any]]:
    if key not in _intrinsic_cache:
        return None
    _intrinsic_cache.move_to_end(key)
    return deepcopy(_intrinsic_cache[key])


def set_intrinsic_cached(key: Tuple, value: Dict[str, Any]) -> None:
    _intrinsic_cache[key] = deepcopy(value)
    _intrinsic_cache.move_to_end(key)
    while len(_intrinsic_cache) > _MAX_ENTRIES:
        _intrinsic_cache.popitem(last=False)


def clear_segment_layout_cache() -> None:
    """Сброс кэша (тесты или смена логики раскладки)."""
    _intrinsic_cache.clear()
