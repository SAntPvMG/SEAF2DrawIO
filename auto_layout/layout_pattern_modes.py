"""
Общие флаги по имени файла паттернов: для office.yaml и dc.yaml применяется одна модель intrinsic + сетки зон.
"""
from __future__ import annotations

import os

_OFFICE_AND_DC_INTRINSICS = frozenset({'office.yaml', 'dc.yaml'})


def patterns_yaml_uses_interior_layout(patterns_yaml_path: str) -> bool:
    """Те же intrinsic-зоны (DMZ, INT-NET, …), единые полоски LAN, полная сетка segment_origin что и для office."""
    return os.path.basename(patterns_yaml_path).lower() in _OFFICE_AND_DC_INTRINSICS
