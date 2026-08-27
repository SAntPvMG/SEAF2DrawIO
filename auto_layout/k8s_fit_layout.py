"""
Автоподгонка раскладки страниц Kubernetes под фактическое содержимое.

После первичной генерации по шаблону (фиксированные w/h и algo=none у namespace)
несколько namespace/app/pod оказываются в одной точке и вылезают за swimlane кластера.
Этот пост-пасс (до flatten):
  1) раскладывает apps/pods внутри каждого namespace;
  2) ресайзит namespace под содержимое;
  3) раскладывает namespace сеткой внутри кластера;
  4) ресайзит кластер;
  5) вертикально пересобирает кластеры без пересечений;
  6) расширяет группу 001 / pageHeight.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple
import re
import xml.etree.ElementTree as ET

_CLUSTER_SCHEMA = 'seaf.company.ta.services.k8s'
_NS_SCHEMA = 'seaf.company.ta.components.k8s_namespaces'
_HPA_SCHEMA = 'seaf.company.ta.components.k8s_hpa'
_DEP_SCHEMA = 'seaf.company.ta.services.k8s_deployments'
_NODE_SCHEMA = 'seaf.company.ta.components.k8s_nodes'

_CLUSTER_GAP_Y = 40
_CLUSTER_PAD_X = 20
_CLUSTER_PAD_BOTTOM = 16
_NS_GAP_X = 12
_NS_GAP_Y = 12
_NS_PAD = 10
_APP_W = 140
_APP_H = 30
_APP_GAP = 8
_POD_W = 115
_POD_H = 40
_POD_GAP = 8
_HPA_W = 87
_HPA_H = 27
_HPA_GAP = 10
_EMPTY_NS_W = 200
_EMPTY_NS_BODY = 8
_MAX_APP_COLS = 4
_MAX_POD_COLS = 4
_MIN_CLUSTER_W = 480
_MIN_ROW_W = 440
_MAX_LEAF_W = 280
_GENERIC_PAD = 12
_GENERIC_GAP = 10
_MAX_APP_W = 260
_MAX_POD_W = 230
_MAX_HPA_W = 200
# Helvetica: средняя ширина глифа и межстрочный интервал в долях кегля.
_CHAR_W_K = 0.58
_LINE_H_K = 1.3
_TEXT_PAD_X = 8.0
_TEXT_PAD_Y = 3.0


def _geom_num(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f'{value:g}'


def _font_size(style: str, default: float) -> float:
    m = re.search(r'fontSize=(\d+(?:\.\d+)?)', style or '')
    if not m:
        return default
    try:
        return float(m.group(1))
    except (TypeError, ValueError):
        return default


def _label_lines(label: str, base_fs: float) -> List[Tuple[str, float]]:
    """Логические строки подписи: (текст, кегль). Учитывает inline font-size."""
    html = str(label or '')
    parts = re.split(r'(?i)<br\s*/?>|</div>\s*<div[^>]*>', html)
    out: List[Tuple[str, float]] = []
    for part in parts:
        m = re.search(r'(?i)font-size:\s*(\d+(?:\.\d+)?)px', part)
        fs = float(m.group(1)) if m else base_fs
        text = re.sub(r'<[^>]+>', '', part)
        text = (text.replace('&nbsp;', ' ').replace('&amp;', '&')
                .replace('&lt;', '<').replace('&gt;', '>').replace('&quot;', '"'))
        text = ' '.join(text.split())
        if text:
            out.append((text, fs))
    return out


def _fit_box_size(
    label: str,
    style: str,
    base_w: float,
    base_h: float,
    max_w: float,
    default_fs: float,
) -> Tuple[float, float]:
    """Размер бокса, при котором подпись не выходит за контур."""
    base_fs = _font_size(style, default_fs)
    lines = _label_lines(label, base_fs)
    if not lines:
        return base_w, base_h
    widest = max(len(text) * fs * _CHAR_W_K for text, fs in lines)
    w = min(max_w, max(base_w, widest + 2 * _TEXT_PAD_X))
    inner = max(24.0, w - 2 * _TEXT_PAD_X)
    height = 0.0
    for text, fs in lines:
        need = len(text) * fs * _CHAR_W_K
        rows = max(1, int(-(-need // inner)))
        height += rows * fs * _LINE_H_K
    return w, max(base_h, height + 2 * _TEXT_PAD_Y)


def _parse_start_size(style: str, default: float = 40.0) -> float:
    m = re.search(r'startSize=(\d+(?:\.\d+)?)', style or '')
    if not m:
        return default
    try:
        return float(m.group(1))
    except (TypeError, ValueError):
        return default


def _set_xywh(geom: ET.Element, x: float, y: float, w: float, h: float) -> None:
    geom.set('x', _geom_num(x))
    geom.set('y', _geom_num(y))
    geom.set('width', _geom_num(max(1.0, w)))
    geom.set('height', _geom_num(max(1.0, h)))


def _cell_maps(root: ET.Element) -> Tuple[
    Dict[str, ET.Element],
    Dict[str, ET.Element],
    Dict[str, ET.Element],
]:
    """oid → object, oid → mxCell, oid → mxGeometry."""
    objects: Dict[str, ET.Element] = {}
    cells: Dict[str, ET.Element] = {}
    geoms: Dict[str, ET.Element] = {}
    for el in list(root):
        oid = el.get('id')
        if not oid:
            continue
        if el.tag == 'object':
            mx = el.find('mxCell')
            if mx is None:
                continue
            objects[oid] = el
            cells[oid] = mx
            g = mx.find('mxGeometry')
            if g is not None:
                geoms[oid] = g
        elif el.tag == 'mxCell' and el.get('vertex') == '1':
            cells[oid] = el
            g = el.find('mxGeometry')
            if g is not None:
                geoms[oid] = g
    return objects, cells, geoms


def _title_min_width(
    label: str,
    style: str,
    avail_h: float,
    default_fs: float,
    w_lo: float,
    w_hi: float,
) -> float:
    """Минимальная ширина, при которой заголовок swimlane влезает в шапку."""
    for w in range(int(w_lo), int(w_hi) + 20, 20):
        _, need_h = _fit_box_size(label, style, float(w), 0.0, float(w), default_fs)
        if need_h - 2 * _TEXT_PAD_Y <= avail_h:
            return float(w)
    return float(w_hi)


def _ns_title_width(
    ns_oid: str,
    objects: Dict[str, ET.Element],
    ns_cell: ET.Element,
    start: float,
    current_w: float,
) -> float:
    obj = objects.get(ns_oid)
    label = (obj.get('label') if obj is not None else None) or ns_cell.get('value') or ''
    return _title_min_width(
        label, ns_cell.get('style') or '', max(12.0, start - 6.0), 9.0,
        current_w, 640.0,
    )


def _sized_boxes(
    oids: List[str],
    objects: Dict[str, ET.Element],
    cells: Dict[str, ET.Element],
    base_w: float,
    base_h: float,
    max_w: float,
    default_fs: float,
) -> List[Tuple[str, float, float]]:
    """(oid, w, h) с размерами, достаточными для подписи."""
    boxes: List[Tuple[str, float, float]] = []
    for oid in oids:
        mx = cells.get(oid)
        obj = objects.get(oid)
        label = (obj.get('label') if obj is not None else None) or (
            mx.get('value') if mx is not None else '')
        style = mx.get('style') if mx is not None else ''
        w, h = _fit_box_size(label, style or '', base_w, base_h, max_w, default_fs)
        boxes.append((oid, w, h))
    return boxes


def _layout_namespace(
    ns_oid: str,
    objects: Dict[str, ET.Element],
    cells: Dict[str, ET.Element],
    geoms: Dict[str, ET.Element],
    child_apps: List[str],
    child_pods: List[str],
) -> Tuple[float, float]:
    """Раскладка apps/pods внутри namespace; возвращает (w, h)."""
    ns_cell = cells[ns_oid]
    ns_geom = geoms[ns_oid]
    start = _parse_start_size(ns_cell.get('style') or '', 40.0)
    apps = sorted(child_apps)
    pods = sorted(child_pods)

    if not apps and not pods:
        w = max(
            float(_EMPTY_NS_W),
            _ns_title_width(ns_oid, objects, ns_cell, start, float(_EMPTY_NS_W)),
        )
        h = start + _EMPTY_NS_BODY
        _set_xywh(
            ns_geom,
            float(ns_geom.get('x') or 0),
            float(ns_geom.get('y') or 0),
            w,
            h,
        )
        return w, h

    app_boxes = _sized_boxes(apps, objects, cells, _APP_W, _APP_H, _MAX_APP_W, 9.0)
    pod_boxes = _sized_boxes(pods, objects, cells, _POD_W, _POD_H, _MAX_POD_W, 8.0)
    app_cols = min(_MAX_APP_COLS, max(1, len(apps))) if apps else 1
    pod_cols = min(_MAX_POD_COLS, max(1, len(pods))) if pods else 1
    row_limit = max(
        app_cols * max([w for _, w, _ in app_boxes] or [0.0])
        + max(0, app_cols - 1) * _APP_GAP,
        pod_cols * max([w for _, w, _ in pod_boxes] or [0.0])
        + max(0, pod_cols - 1) * _POD_GAP,
        160.0,
    )

    y = start + 8.0
    x0 = float(_NS_PAD)
    used_w = 0.0
    for boxes, gap in ((app_boxes, _APP_GAP), (pod_boxes, _POD_GAP)):
        if not boxes:
            continue
        places, content_w, content_h = _flow_place_boxes(
            boxes, x0, y, gap, gap, row_limit,
        )
        sizes = {oid: (w, h) for oid, w, h in boxes}
        for oid, bx, by in places:
            bw, bh = sizes[oid]
            _set_xywh(geoms[oid], bx, by, bw, bh)
        used_w = max(used_w, content_w)
        y += content_h + gap
    y -= _POD_GAP if pod_boxes else _APP_GAP

    w = max(used_w + 2 * _NS_PAD, 180.0)
    w = max(w, _ns_title_width(ns_oid, objects, ns_cell, start, w))
    h = y + _NS_PAD
    _set_xywh(
        ns_geom,
        float(ns_geom.get('x') or 0),
        float(ns_geom.get('y') or 0),
        w,
        h,
    )
    return w, h


def _flow_place_boxes(
    boxes: List[Tuple[str, float, float]],
    origin_x: float,
    origin_y: float,
    gap_x: float,
    gap_y: float,
    max_row_w: float,
) -> Tuple[List[Tuple[str, float, float]], float, float]:
    """
    boxes: (oid, w, h). Раскладка с переносом строки.
    Возвращает (placements oid,x,y), content_w, content_h.
    """
    if not boxes:
        return [], 0.0, 0.0
    x = origin_x
    y = origin_y
    row_h = 0.0
    row_start_x = origin_x
    max_x = origin_x
    out: List[Tuple[str, float, float]] = []
    for oid, w, h in boxes:
        if x > row_start_x and (x + w - row_start_x) > max_row_w:
            x = row_start_x
            y += row_h + gap_y
            row_h = 0.0
        out.append((oid, x, y))
        max_x = max(max_x, x + w)
        x += w + gap_x
        row_h = max(row_h, h)
    content_w = max_x - origin_x
    content_h = (y + row_h) - origin_y
    return out, content_w, content_h


def _layout_cluster(
    cluster_oid: str,
    objects: Dict[str, ET.Element],
    cells: Dict[str, ET.Element],
    geoms: Dict[str, ET.Element],
) -> None:
    cluster_cell = cells[cluster_oid]
    cluster_geom = geoms[cluster_oid]
    start = _parse_start_size(cluster_cell.get('style') or '', 50.0)

    ns_oids: List[str] = []
    hpa_oids: List[str] = []
    for oid, obj in objects.items():
        mx = cells.get(oid)
        if mx is None or mx.get('parent') != cluster_oid:
            continue
        schema = obj.get('schema') or ''
        if schema == _NS_SCHEMA:
            ns_oids.append(oid)
        elif schema == _HPA_SCHEMA:
            hpa_oids.append(oid)

    # Дети namespace (apps / pods) — по parent.
    apps_by_ns: Dict[str, List[str]] = {n: [] for n in ns_oids}
    pods_by_ns: Dict[str, List[str]] = {n: [] for n in ns_oids}
    for oid, obj in objects.items():
        mx = cells.get(oid)
        if mx is None:
            continue
        parent = mx.get('parent') or ''
        if parent not in apps_by_ns:
            continue
        if oid.endswith('__app') or (
            (obj.get('schema') or '') == _DEP_SCHEMA and oid.endswith('__app')
        ):
            apps_by_ns[parent].append(oid)
        elif oid.endswith('__pod') or oid.endswith('__pod1') or oid.endswith('__pod2'):
            pods_by_ns[parent].append(oid)

    ns_sizes: Dict[str, Tuple[float, float]] = {}
    for ns_oid in ns_oids:
        ns_sizes[ns_oid] = _layout_namespace(
            ns_oid, objects, cells, geoms, apps_by_ns[ns_oid], pods_by_ns[ns_oid],
        )

    # Сначала namespace с содержимым (шире), потом пустые.
    def ns_sort_key(oid: str) -> Tuple[int, str]:
        apps_n = len(apps_by_ns.get(oid) or [])
        pods_n = len(pods_by_ns.get(oid) or [])
        return (0 if (apps_n + pods_n) > 0 else 1, oid)

    ordered = sorted(ns_oids, key=ns_sort_key)
    boxes = [(oid, ns_sizes[oid][0], ns_sizes[oid][1]) for oid in ordered]

    # Целевая ширина ряда: не уже минимального кластера, но растёт с широким namespace.
    widest = max((b[1] for b in boxes), default=400.0)
    row_budget = max(_MIN_CLUSTER_W - 2 * _CLUSTER_PAD_X, widest)

    origin_x = float(_CLUSTER_PAD_X)
    origin_y = start + 10.0
    places, content_w, content_h = _flow_place_boxes(
        boxes, origin_x, origin_y, _NS_GAP_X, _NS_GAP_Y, row_budget,
    )
    for oid, x, y in places:
        g = geoms[oid]
        _set_xywh(g, x, y, ns_sizes[oid][0], ns_sizes[oid][1])

    bottom = origin_y + content_h
    if hpa_oids:
        bottom += _HPA_GAP
        hpa_boxes = _sized_boxes(
            sorted(hpa_oids), objects, cells, _HPA_W, _HPA_H, _MAX_HPA_W, 8.0,
        )
        row_budget_hpa = max(row_budget, max(w for _, w, _ in hpa_boxes))
        places_hpa, hpa_w, hpa_h = _flow_place_boxes(
            hpa_boxes, origin_x, bottom, _HPA_GAP, _HPA_GAP, row_budget_hpa,
        )
        sizes = {oid: (w, h) for oid, w, h in hpa_boxes}
        for oid, hx, hy in places_hpa:
            bw, bh = sizes[oid]
            _set_xywh(geoms[oid], hx, hy, bw, bh)
        content_w = max(content_w, hpa_w)
        bottom += hpa_h

    cluster_w = max(_MIN_CLUSTER_W, content_w + 2 * _CLUSTER_PAD_X)
    cluster_h = bottom + _CLUSTER_PAD_BOTTOM
    _set_xywh(
        cluster_geom,
        float(cluster_geom.get('x') or 0),
        float(cluster_geom.get('y') or 0),
        cluster_w,
        cluster_h,
    )


def _restack_clusters(
    cluster_oids: List[str],
    geoms: Dict[str, ET.Element],
) -> Tuple[float, float]:
    """Вертикальная стопка без пересечений. Возвращает (max_right, max_bottom)."""
    items = []
    for oid in cluster_oids:
        g = geoms[oid]
        items.append((
            oid,
            float(g.get('x') or 0),
            float(g.get('y') or 0),
            float(g.get('width') or 0),
            float(g.get('height') or 0),
        ))
    items.sort(key=lambda t: (t[2], t[1], t[0]))
    if not items:
        return 0.0, 0.0

    y_cursor = items[0][2]
    x0 = items[0][1]
    max_right = 0.0
    max_bottom = 0.0
    for oid, _x, _y, w, h in items:
        g = geoms[oid]
        _set_xywh(g, x0, y_cursor, w, h)
        max_right = max(max_right, x0 + w)
        max_bottom = max(max_bottom, y_cursor + h)
        y_cursor += h + _CLUSTER_GAP_Y
    return max_right, max_bottom


def _descendants(
    roots: List[str],
    cells: Dict[str, ET.Element],
) -> set:
    """Все вложенные ячейки указанных контейнеров."""
    inside = set(roots)
    grew = True
    while grew:
        grew = False
        for oid, mx in cells.items():
            if oid in inside:
                continue
            if (mx.get('parent') or '') in inside:
                inside.add(oid)
                grew = True
    return inside - set(roots)


def _children_map(cells: Dict[str, ET.Element]) -> Dict[str, List[str]]:
    """parent → дети в порядке документа (порядок задаёт секции в middle/full)."""
    out: Dict[str, List[str]] = {}
    for oid, mx in cells.items():
        out.setdefault(mx.get('parent') or '', []).append(oid)
    return out


def _has_unmanaged_children(
    cluster_oids: List[str],
    objects: Dict[str, ET.Element],
    cells: Dict[str, ET.Element],
) -> bool:
    """Есть ли внутри кластеров объекты, которыми minimal-раскладка не управляет."""
    inside = set(cluster_oids)
    grew = True
    while grew:
        grew = False
        for oid, mx in cells.items():
            if oid in inside:
                continue
            if (mx.get('parent') or '') in inside:
                inside.add(oid)
                grew = True
    for oid in inside - set(cluster_oids):
        schema = (objects.get(oid).get('schema') or '') if oid in objects else ''
        if schema == _NODE_SCHEMA or oid.endswith('_label'):
            return True
    return False


def _is_section_header(oid: str, cells: Dict[str, ET.Element]) -> bool:
    """Подпись секции: занимает свой ряд целиком."""
    style = cells[oid].get('style') or ''
    return oid.endswith('_label') or style.startswith('text;')


def _label_of(oid: str, objects: Dict[str, ET.Element], cells: Dict[str, ET.Element]) -> str:
    obj = objects.get(oid)
    if obj is not None and obj.get('label'):
        return obj.get('label') or ''
    return cells[oid].get('value') or ''


def _layout_generic(
    oid: str,
    objects: Dict[str, ET.Element],
    cells: Dict[str, ET.Element],
    geoms: Dict[str, ET.Element],
    children: Dict[str, List[str]],
) -> Tuple[float, float]:
    """
    Рекурсивная раскладка контейнера по фактическому содержимому.
    Подписи секций и вложенные контейнеры занимают отдельные ряды, остальные
    объекты выстраиваются в поток с переносом. Возвращает (w, h).
    """
    geom = geoms[oid]
    style = cells[oid].get('style') or ''
    cur_w = float(geom.get('width') or 0)
    cur_h = float(geom.get('height') or 0)
    kids = [k for k in children.get(oid, []) if k in geoms]

    if not kids:
        if 'swimlane' in style:
            # Пустой swimlane (namespace без объектов) — только шапка.
            start_empty = _parse_start_size(style, 30.0)
            w_empty = _title_min_width(
                _label_of(oid, objects, cells), style,
                max(12.0, start_empty - 6.0), 9.0, float(_EMPTY_NS_W), 720.0,
            )
            return w_empty, start_empty + _EMPTY_NS_BODY
        return _fit_box_size(
            _label_of(oid, objects, cells), style,
            cur_w, cur_h, max(cur_w, _MAX_LEAF_W), 9.0,
        )

    start = _parse_start_size(style, 0.0) if 'swimlane' in style else 0.0
    sizes: Dict[str, Tuple[float, float]] = {}
    for kid in kids:
        sizes[kid] = _layout_generic(kid, objects, cells, geoms, children)

    # Вложенные контейнеры (namespace) выстраиваются в несколько колонок, иначе
    # кластер с десятками namespace вытягивается в бесконечную полосу.
    nested = [k for k in kids if children.get(k)]
    cols = 1 if len(nested) <= 1 else (2 if len(nested) <= 6 else 3)
    widest_nested = max([sizes[k][0] for k in nested], default=0.0)
    row_budget = max(
        _MIN_ROW_W,
        max(w for w, _ in sizes.values()),
        cols * widest_nested + (cols - 1) * _GENERIC_GAP,
    )
    pad = float(_GENERIC_PAD)
    x = pad
    y = start + pad
    row_h = 0.0
    content_w = 0.0
    for kid in kids:
        w, h = sizes[kid]
        own_row = _is_section_header(kid, cells)
        wrap = x > pad and (x + w - pad) > row_budget
        if (own_row and x > pad) or wrap:
            x = pad
            y += row_h + _GENERIC_GAP
            row_h = 0.0
        _set_xywh(geoms[kid], x, y, w, h)
        content_w = max(content_w, x + w - pad)
        if own_row:
            y += h + _GENERIC_GAP
            x = pad
            row_h = 0.0
        else:
            x += w + _GENERIC_GAP
            row_h = max(row_h, h)
    bottom = y + row_h

    w = content_w + 2 * pad
    if 'swimlane' in style:
        w = max(w, _title_min_width(
            _label_of(oid, objects, cells), style, max(12.0, start - 6.0), 9.0, w, 720.0,
        ))
    return w, bottom + pad


def fit_k8s_clusters_to_content(diagram: Any, page_name: str) -> None:
    """
    Подгоняет namespace/кластеры на странице K8s под число объектов.
    Вызывать до _flatten_k8s_page, пока сохранена иерархия parent.
    """
    diagram.go_to_diagram(diagram_name=page_name)
    root = diagram.current_root
    objects, cells, geoms = _cell_maps(root)

    cluster_oids = [
        oid for oid, obj in objects.items()
        if (obj.get('schema') or '') == _CLUSTER_SCHEMA
        and '__' not in oid
        and oid in geoms
    ]
    if not cluster_oids:
        return

    children = _children_map(cells)
    if _has_unmanaged_children(cluster_oids, objects, cells):
        # middle/full: внутри кластеров есть узлы и подписи секций.
        for coid in cluster_oids:
            w, h = _layout_generic(coid, objects, cells, geoms, children)
            g = geoms[coid]
            _set_xywh(g, float(g.get('x') or 0), float(g.get('y') or 0),
                      max(w, _MIN_CLUSTER_W), h)
    else:
        for coid in cluster_oids:
            _layout_cluster(coid, objects, cells, geoms)

    # Стопка по всем блокам верхнего уровня: помимо кластеров там бывают
    # swimlane общей инфраструктуры, их нельзя оставить на прежних y.
    parents = {cells[c].get('parent') or '' for c in cluster_oids}
    top_level = [
        oid for oid in children.get(next(iter(parents)), [])
        if oid in geoms and (oid in cluster_oids or oid not in _descendants(cluster_oids, cells))
    ] if len(parents) == 1 else cluster_oids
    max_right, max_bottom = _restack_clusters(top_level or cluster_oids, geoms)

    # Рамка группы 001.
    for el in root.iter('mxCell'):
        if el.get('id') != '001':
            continue
        g = el.find('mxGeometry')
        if g is None:
            break
        pad = 40.0
        try:
            cur_w = float(g.get('width') or 0)
            cur_h = float(g.get('height') or 0)
        except (TypeError, ValueError):
            cur_w = cur_h = 0.0
        g.set('width', _geom_num(max(cur_w, max_right + pad)))
        g.set('height', _geom_num(max(cur_h, max_bottom + pad)))
        break

    # pageHeight / pageWidth у mxGraphModel.
    diagram_el = getattr(diagram, 'current_diagram', None)
    if diagram_el is not None:
        model = diagram_el.find('mxGraphModel')
        if model is not None:
            try:
                ph = float(model.get('pageHeight') or 0)
                pw = float(model.get('pageWidth') or 0)
            except (TypeError, ValueError):
                ph = pw = 0.0
            model.set('pageHeight', _geom_num(max(ph, max_bottom + 80)))
            model.set('pageWidth', _geom_num(max(pw, max_right + 80)))
