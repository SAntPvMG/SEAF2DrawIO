from N2G import drawio_diagram
import sys
import json
import re
import os
import argparse
from copy import deepcopy
import textwrap
from lib import seaf_drawio
from lib.link_manager import remove_obsolete_links, draw_verify, advanced_analysis
from auto_layout.edge_segments_layout import (
    edge_segments_layout as compute_wan_edge_layout,
    resolve_page_location_roots,
)
from auto_layout.dmz_segments_layout import dmz_segments_layout as compute_dmz_layout
from auto_layout.segment_intrinsic_layout import align_int_net_security_bottom_to_int_wan_edge
from auto_layout.kb_layout import kb_layout
from auto_layout.services_ta_layout import services_TA_layout
import xml.etree.ElementTree as ET

patterns_dir = 'data/patterns/'
_main_isp_layout_cache = None
diagram = drawio_diagram()
node_xml_default = diagram.drawio_node_object_xml
# Ключ схемы в объединённых YAML (см. data/example/dc_region.yaml)
root_object = 'seaf.company.ta.services.dc_regions'
diagram_pages = {'main': ['Main Schema'], 'office': [], 'dc': [], 'k8s': []}
diagram_ids = {'Main Schema': []}
conf = {}
pending_missing_links = set()
layout_counters = {}
expected_counts = {}
expected_data = {}
pattern_specs = {}
wan_edge_layout_cache = {'positions': {}, 'segment_size': {}, 'segment_origin': {}}

# Режим детализации K8s (data/patterns/k8s.yaml → «Diagram details»)
_k8s_diagram_details = 'full'
_k8s_hpa_by_target = {}
_k8s_clusters_with_hpa = set()
_k8s_worker_count_by_cluster = {}
_k8s_deployment_namespace = {}
# Одна страница для всех кластеров (data/patterns/k8s.yaml → «On one page»)
_k8s_on_one_page = False
_k8s_unified_page_title = 'Kubernetes (все кластеры)'

_K8S_YAML_META_KEYS = frozenset({'Diagram details', 'On one page', 'On one page title'})
DEFAULT_CONFIG = {
    "seaf2drawio": {
        "data_yaml_file": "data/example/test_seaf_ta_P41_v0.9.yaml",
        "drawio_pattern": "data/base.drawio",
        "output_file": "result/Sample_graph.drawio",
        "verify_generation": False
    }
}

d = seaf_drawio.SeafDrawio(DEFAULT_CONFIG)

def _validate_data_yaml_source(path):
    """Файл .yaml/.yml или каталог с такими файлами (как в data_yaml_file)."""
    p = os.path.expanduser(path)
    if os.path.isdir(p):
        if not os.access(p, os.R_OK | os.X_OK):
            raise argparse.ArgumentTypeError(f"Каталог недоступен: {path}")
        return path
    if os.path.isfile(p):
        if not re.search(r"\.(yaml|yml)\Z", path, re.I):
            raise argparse.ArgumentTypeError(f"Ожидается .yaml/.yml или каталог с ними: {path}")
        if not os.access(p, os.R_OK):
            raise argparse.ArgumentTypeError(f"Файл недоступен для чтения: {path}")
        return path
    raise argparse.ArgumentTypeError(f"Файл или каталог не найден: {path}")

def cli_vars(config):
    try:
        parser = argparse.ArgumentParser(description="Параметры командной строки.")

        dst_validator = d.create_validator(r'^.+(\.drawio)$')

        parser.add_argument("-s", "--src", type=_validate_data_yaml_source, help="файл или каталог YAML данных SEAF",
                            required=False)
        parser.add_argument("-d", "--dst", type=dst_validator, help="путь и имя файла вывода результатов",
                            required=False)
        parser.add_argument("-p", "--pattern", type=dst_validator, action=seaf_drawio.ValidateFile, help="шаблон drawio",
                            required=False)
        args = parser.parse_args()
        if args.src:
            config['data_yaml_file'] = args.src
        if args.dst:
            config['output_file'] = args.dst
        if args.pattern:
            config['drawio_pattern'] = args.pattern
        return config

    except argparse.ArgumentTypeError as e:
        print(e)
        sys.exit(1)

def position_offset(pattern):

    match pattern['algo']:
        # По оси Y cверху вниз относительно родительского объекта
        case 'Y+':
            if return_ready(pattern):
                pattern['x'] = pattern['x'] + pattern['w'] + pattern['offset']
                pattern['y'] = pattern['y'] - (pattern['h'] + pattern['offset']) * pattern['deep']
            pattern['y'] = pattern['y'] + pattern['h'] + pattern['offset']

        # Только вниз по Y (без смены колонки X). Для Y+ при deep=1 сдвиг по Y взаимно гасится —
        # несколько корневых кластеров оказывались в одной строке; Y_stack — вертикальная стопка.
        case 'Y_stack':
            pattern['y'] = pattern['y'] + pattern['h'] + pattern['offset']

        case 'Y-':
            if return_ready(pattern):
                pattern['x'] = pattern['x'] + pattern['w'] + pattern['offset']
                pattern['y'] = pattern['y'] + (pattern['h'] + pattern['offset']) * pattern['deep']
            pattern['y'] = pattern['y'] - pattern['h'] - pattern['offset']

        case 'X-':

            if return_ready(pattern):
                pattern['y'] = pattern['y'] +  pattern['h'] + pattern['offset']
                pattern['x'] = pattern['x'] + (pattern['w'] + pattern['offset']) * pattern['deep']
            pattern['x'] = pattern['x'] - pattern['w'] - pattern['offset']
        # По оси X слева направо
        case 'X+':
            if return_ready(pattern):
                pattern['y'] = pattern['y'] +  pattern['h'] + pattern['offset']
                pattern['x'] = pattern['x'] - (pattern['w'] + pattern['offset']) * pattern['deep']
            pattern['x'] = pattern['x'] + pattern['w'] + pattern['offset']

def return_ready(pattern):
    pattern['count']+=1
    if pattern['count'] == pattern['deep']:
        pattern['count'] = 0

    return not bool(pattern['count'])


def _diagram_root_block_for_object_id(root: ET.Element, node_id: str):
    """
    Первый прямой потомок <root>, в котором встречается id у <object> или <mxCell> — порядок в XML задаёт Z в draw.io.
    """
    for child in list(root):
        if child.get('id') == node_id:
            return child
        for el in child.iter():
            if el.get('id') == node_id:
                return child
    return None


def bring_cross_segment_firewalls_to_front(diagram, page_name: str, node_ids) -> None:
    """
    Выводит элементы диаграммы поверх предыдущих (в модели последний sibling рисуется последним — ToFront в XML).
    """
    if not node_ids:
        return
    diagram.go_to_diagram(diagram_name=page_name)
    tree_root = diagram.current_root
    ordered_ids = sorted(node_ids)
    seen_block = set()
    blocks = []
    for oid in ordered_ids:
        block = _diagram_root_block_for_object_id(tree_root, oid)
        if block is None:
            continue
        bid = id(block)
        if bid in seen_block:
            continue
        seen_block.add(bid)
        blocks.append(block)
    for block in blocks:
        tree_root.remove(block)
        tree_root.append(block)


# Сегменты INET-EDGE / EXT-WAN-EDGE должны рисоваться раньше DMZ: иначе серый контейнер после DMZ в XML перекрывает
# NGFW с parent=DMZ и отрицательным x (на стыке с INET). См. jupiter.network_segment.dc(.dc02).(inet|ext_wan)_edge vs .dmz
_OID_NS_INET_EXT = re.compile(r'^jupiter\.network_segment\.[^.]+\.(inet_edge|ext_wan_edge)$')
_OID_NS_DMZ = re.compile(r'^jupiter\.network_segment\.[^.]+\.dmz$')


def reorder_inet_ext_wan_edge_before_dmz_swimlane(diagram, page_name: str) -> None:
    """
    Среди прямых потомков <root> переносит object сегментов INET-EDGE и EXT-WAN-EDGE перед первым object DMZ,
    чтобы полоса DMZ и пограничные FW рисовались поверх серых зон.
    """
    diagram.go_to_diagram(diagram_name=page_name)
    root = diagram.current_root
    edge_objs: list = []
    dmz_oid = None  # первый oid сегмента DMZ под root
    for ch in list(root):
        if ch.tag != 'object':
            continue
        oid = ch.get('id') or ''
        if _OID_NS_INET_EXT.match(oid):
            edge_objs.append(ch)
        elif _OID_NS_DMZ.match(oid) and dmz_oid is None:
            dmz_oid = oid
    if not edge_objs or not dmz_oid:
        return

    def _edge_layer_order(o) -> tuple:
        oid = (o.get('id') or '').rsplit('.', 1)[-1]
        return (0 if oid == 'inet_edge' else 1, o.get('id') or '')

    edge_objs.sort(key=_edge_layer_order)
    for e in edge_objs:
        root.remove(e)
    children = list(root)
    dmz_idx = None
    for i, ch in enumerate(children):
        if ch.tag == 'object' and ch.get('id') == dmz_oid:
            dmz_idx = i
            break
    if dmz_idx is None:
        return
    for offset, e in enumerate(edge_objs):
        root.insert(dmz_idx + offset, e)


_SEGMENT_PARENT_CELL_ID = '001'
_SWIMLANE_GROUP_CELL_ID = '001'
_LABEL_TO_SEGMENT_PAD = 5
_LABEL_STENCIL_MARK = 'vsdxID=13090'

# Оценка подписи ярлыка (Calibri в шаблоне dc_label/office_label: заголовок 16.93px, текст 11.29px, line-height 120%)
_TITLE_FONT_PX = 16.93
_BODY_FONT_PX = 11.29
_LABEL_LINE_HEIGHT_MULT = 1.2
_LABEL_PAD_X = 18
_LABEL_PAD_Y = 14
_LABEL_GAP_ABOVE_SEGMENTS = 10
_LABEL_SHIFT_DOWN_PX = 30


def _label_char_px(font_px: float) -> float:
    return max(5.0, font_px * 0.52)


def _label_chars_per_line(max_inner_px: float, font_px: float) -> int:
    return max(10, int(max_inner_px / _label_char_px(font_px)))


def _label_wrap_lines(text: str, max_inner_px: float, font_px: float) -> list[str]:
    if not (text or '').strip():
        return []
    cpl = _label_chars_per_line(max_inner_px, font_px)
    lines: list[str] = []
    for para in text.replace('\r\n', '\n').replace('\r', '\n').split('\n'):
        para = para.strip()
        if not para:
            lines.append('')
            continue
        wrapped = textwrap.wrap(para, width=cpl, break_long_words=True, replace_whitespace=False)
        lines.extend(wrapped if wrapped else [''])
    return lines


def _location_label_text_box_px(attribs: dict, file_name: str, inner_max_px: float) -> tuple[float, float]:
    """Приблизительная ширина/высота блока текста ярлыка (без растягивания на весь swimlane)."""
    title = (attribs.get('title') or '').strip()
    if file_name == 'dc':
        body = (attribs.get('description') or '').strip()
    else:
        body = (attribs.get('address') or '').strip()

    inner_max_px = max(60.0, float(inner_max_px))

    title_lines = _label_wrap_lines(title, inner_max_px, _TITLE_FONT_PX) if title else []
    body_lines = _label_wrap_lines(body, inner_max_px, _BODY_FONT_PX) if body else []

    tw = 0.0
    for ln in title_lines:
        tw = max(tw, len(ln) * _label_char_px(_TITLE_FONT_PX))
    for ln in body_lines:
        tw = max(tw, len(ln) * _label_char_px(_BODY_FONT_PX))

    line_h_title = _TITLE_FONT_PX * _LABEL_LINE_HEIGHT_MULT
    line_h_body = _BODY_FONT_PX * _LABEL_LINE_HEIGHT_MULT
    th = len(title_lines) * line_h_title + len(body_lines) * line_h_body
    if title_lines and body_lines:
        th += max(4.0, _BODY_FONT_PX * 0.35)

    if not title_lines and not body_lines:
        return float(_LABEL_PAD_X * 2), float(_LABEL_PAD_Y * 2)

    w_px = tw + _LABEL_PAD_X * 2
    h_px = th + _LABEL_PAD_Y * 2
    return w_px, h_px


def _mx_cell_geometry_bounds(mx_cell):
    geom = mx_cell.find('mxGeometry')
    if geom is None:
        return None
    if (geom.get('relative') or '').strip() == '1':
        return None
    try:
        x = float(geom.get('x') or 0)
        y = float(geom.get('y') or 0)
        w = float(geom.get('width') or 0)
        h = float(geom.get('height') or 0)
    except (TypeError, ValueError):
        return None
    if w <= 0 or h <= 0:
        return None
    return x, y, x + w, y + h


def _is_segment_zone_swimlane_mx_cell(mx_cell):
    """Контейнер зоны под группой ЦОД/офиса: parent=001, container=1, не ярлык vsdxID=13090."""
    if mx_cell.get('parent') != _SEGMENT_PARENT_CELL_ID:
        return False
    if mx_cell.get('vertex') != '1':
        return False
    if mx_cell.get('edge') == '1':
        return False
    style = mx_cell.get('style') or ''
    if 'container=1' not in style:
        return False
    if _LABEL_STENCIL_MARK in style:
        return False
    return True


def _translate_mx_geometry_by(mx_geom, ddx: float, ddy: float):
    try:
        x = float(mx_geom.get('x') or 0)
        y = float(mx_geom.get('y') or 0)
    except (TypeError, ValueError):
        return
    mx_geom.set('x', str(int(round(x - ddx))))
    mx_geom.set('y', str(int(round(y - ddy))))


def _find_swimlane_group_mx_cell(root):
    for ch in root:
        if ch.tag == 'mxCell' and ch.get('id') == _SWIMLANE_GROUP_CELL_ID:
            return ch
    return None


def _location_label_ids_for_page(diagram, sd, conf, page_name, label_schema, diagram_ids_map):
    """
    OID ярлыка ЦОД/офиса на странице: пересечение с diagram_ids, иначе по title через main.yaml (ext_page),
    иначе по object[@schema] на самой диаграмме.
    """
    location_keys = set(sd.get_object(conf['data_yaml_file'], label_schema).keys())
    page_ids = set(diagram_ids_map.get(page_name) or [])
    ids = location_keys & page_ids
    if ids:
        return ids
    roots_main = resolve_page_location_roots(sd, conf, page_name, patterns_dir + 'main.yaml')
    ids = location_keys & set(roots_main)
    if ids:
        return ids
    diagram.go_to_diagram(diagram_name=page_name)
    root = diagram.current_root
    found = set()
    for child in root:
        if child.tag != 'object':
            continue
        if (child.get('schema') or '') != label_schema:
            continue
        oid = child.get('id')
        if oid:
            found.add(oid)
    return found


def resize_location_label_to_cover_segments(diagram, page_name, file_name, diagram_ids_map, conf, sd):
    """
    После раскладки сегментов:
    — bbox по контейнерам зон (mxCell parent «001», style container=1), без ярлыка vsdxID=13090;
    — сдвиг вершин с parent «001» (кроме ярлыка), подгонка mxCell id «001» под контент + отступ;
    — ярлык ЦОД/офиса: размеры по оценке текста (title + description/address), без совпадения с bbox сегментов;
      позиция над верхом зон; атрибут label (HTML) не меняется.
    """
    if file_name not in ('dc', 'office'):
        return
    patterns_doc = sd.read_yaml_file(patterns_dir + file_name + '.yaml')
    label_pat = patterns_doc.get('dc_label') if file_name == 'dc' else patterns_doc.get('office_label')
    if not isinstance(label_pat, dict) or not label_pat.get('schema'):
        return
    label_schema = label_pat['schema']

    diagram.go_to_diagram(diagram_name=page_name)
    root = diagram.current_root

    label_ids = _location_label_ids_for_page(diagram, sd, conf, page_name, label_schema, diagram_ids_map)
    if not label_ids:
        return

    pad = _LABEL_TO_SEGMENT_PAD
    min_x = min_y = float('inf')
    max_x = max_y = float('-inf')
    seen = False

    for top in root:
        if top.tag != 'object':
            continue
        oid = top.get('id') or ''
        if oid in label_ids:
            continue
        for cell in top.iter('mxCell'):
            if not _is_segment_zone_swimlane_mx_cell(cell):
                continue
            bounds = _mx_cell_geometry_bounds(cell)
            if bounds is None:
                continue
            lx, ly, rx, ry = bounds
            min_x = min(min_x, lx)
            min_y = min(min_y, ly)
            max_x = max(max_x, rx)
            max_y = max(max_y, ry)
            seen = True

    if not seen:
        return

    ddx = min_x - pad
    ddy = min_y - pad
    cw = max(max_x - min_x + 2 * pad, 1)
    ch = max(max_y - min_y + 2 * pad, 1)

    for top in root:
        if top.tag == 'object' and (top.get('id') or '') in label_ids:
            continue
        for cell in top.iter('mxCell'):
            if cell.get('parent') != _SEGMENT_PARENT_CELL_ID:
                continue
            if cell.get('vertex') != '1' or cell.get('edge') == '1':
                continue
            geom = cell.find('mxGeometry')
            if geom is None:
                continue
            _translate_mx_geometry_by(geom, ddx, ddy)

    for mx_ch in root:
        if mx_ch.tag != 'mxCell':
            continue
        if mx_ch.get('parent') != _SEGMENT_PARENT_CELL_ID:
            continue
        if mx_ch.get('vertex') != '1' or mx_ch.get('edge') == '1':
            continue
        geom = mx_ch.find('mxGeometry')
        if geom is None:
            continue
        _translate_mx_geometry_by(geom, ddx, ddy)

    group_el = _find_swimlane_group_mx_cell(root)
    if group_el is not None:
        ggeom = group_el.find('mxGeometry')
        if ggeom is not None:
            try:
                gx = float(ggeom.get('x') or 0)
                gy = float(ggeom.get('y') or 0)
            except (TypeError, ValueError):
                gx, gy = 0.0, 0.0
            ggeom.set('x', str(int(round(gx + ddx))))
            ggeom.set('y', str(int(round(gy + ddy))))
            ggeom.set('width', str(int(round(cw))))
            ggeom.set('height', str(int(round(ch))))

    max_label_outer_w = max(int(label_pat.get('w', 120)), int(cw) - 2 * pad)
    inner_max = max(60.0, float(max_label_outer_w - _LABEL_PAD_X * 2))
    min_lw = int(label_pat.get('w', 120))
    min_lh = int(label_pat.get('h', 48))

    for child in root:
        if child.tag != 'object' or child.get('id') not in label_ids:
            continue
        tw_px, th_px = _location_label_text_box_px(dict(child.attrib), file_name, inner_max)
        lw = max(min_lw, int(round(tw_px)))
        lw = min(lw, max_label_outer_w)
        lh = max(min_lh, int(round(th_px)))

        lx = pad
        ly = int(round(pad - lh - _LABEL_GAP_ABOVE_SEGMENTS + _LABEL_SHIFT_DOWN_PX))

        for cell in child.iter('mxCell'):
            if cell.get('parent') != _SEGMENT_PARENT_CELL_ID:
                continue
            geom = cell.find('mxGeometry')
            if geom is None:
                continue
            geom.set('x', str(lx))
            geom.set('y', str(ly))
            geom.set('width', str(lw))
            geom.set('height', str(lh))
            break


def get_parent_value(pattern, current_parent):
    r = ''
    if pattern.get('parent_key'):
        r = d.find_value_by_key(d.find_value_by_key(json.loads(json.dumps(d.read_and_merge_yaml(conf['data_yaml_file']))),
                                                    current_parent), pattern['parent_key'])
    return r


def _get_main_isp_layout():
    """Параметры раскладки isp из data/patterns/main.yaml (без дублирования чисел в коде)."""
    global _main_isp_layout_cache
    if _main_isp_layout_cache is None:
        doc = d.read_yaml_file(patterns_dir + 'main.yaml')
        isp = doc.get('isp') or {}
        _main_isp_layout_cache = {
            'w': int(isp.get('w', 110)),
            'h': int(isp.get('h', 60)),
            'offset': int(isp.get('offset', 25)),
            'deep': int(isp.get('deep', 15)),
            'x': int(isp.get('x', 10)),
            'y': int(isp.get('y', 5)),
        }
    return _main_isp_layout_cache


def _network_segment_refs_match(ndata, segment_oid):
    """segment в данных — строка или список."""
    seg = ndata.get('segment')
    if seg is None:
        return False
    if isinstance(seg, list):
        return segment_oid in seg
    return seg == segment_oid


def compute_main_schema_segment_dimensions(segment_oid, segment_pattern):
    """
    Размеры контейнера segment_internet / segment_transport_wan на Main Schema:
    охватывают все связанные WAN (isp), раскладка как у паттерна isp (Y+, deep колонки).
    """
    lay = _get_main_isp_layout()
    isp_w, isp_h = lay['w'], lay['h']
    isp_off = lay['offset']
    isp_deep = max(1, lay['deep'])
    sx, sy = lay['x'], lay['y']
    # Подпись "… Router" чуть ниже h группы в шаблоне isp
    label_slop = 8

    base_w = int(segment_pattern.get('w', 140))
    base_h = int(segment_pattern.get('h', 350))
    pad = int(segment_pattern.get('offset', 10))

    try:
        nets = d.get_object(
            conf['data_yaml_file'],
            'seaf.company.ta.services.networks',
            type='WAN',
            require_fields=['provider'],
        )
    except Exception:
        return base_w, base_h

    n = sum(1 for oid, nd in nets.items() if _network_segment_refs_match(nd, segment_oid))
    if n == 0:
        return base_w, base_h

    max_right = sx
    max_bottom = sy
    for i in range(n):
        col = i // isp_deep
        row = i % isp_deep
        xi = sx + col * (isp_w + isp_off)
        yi = sy + row * (isp_h + isp_off)
        max_right = max(max_right, xi + isp_w)
        max_bottom = max(max_bottom, yi + isp_h + label_slop)

    inner_w = max_right + pad
    inner_h = max_bottom + pad
    return max(inner_w, base_w), max(inner_h, base_h)


def _is_main_schema_zone_segment(pattern):
    if pattern.get('schema') != 'seaf.company.ta.services.network_segments':
        return False
    t = pattern.get('type') or ''
    return t in ('zone:INTERNET', 'zone:TRANSPORT-WAN')


def _is_segment_auto_size_from_layout(pattern):
    """network_segments: размер контейнера из wan_edge_layout_cache.segment_size."""
    if pattern.get('schema') != 'seaf.company.ta.services.network_segments':
        return False
    t = pattern.get('type') or ''
    if not t.startswith('zone:'):
        return False
    zone = t.split(':', 1)[1]
    return zone in (
        'INT-WAN-EDGE', 'INET-EDGE', 'EXT-WAN-EDGE',
        'DMZ', 'INT-NET', 'INT-SECURITY-NET',
    )


def _normalize_k8s_yes_no(raw) -> bool:
    return str(raw or 'no').strip().lower() in ('yes', 'true', '1', 'да')


def _expand_k8s_ext_page_for_one_page(ext_xml: str, n_clusters: int) -> str:
    """Увеличивает высоту холста под несколько кластеров (вертикальная укладка)."""
    if n_clusters <= 1:
        return ext_xml
    if 'pageHeight="2970"' in ext_xml:
        slot = 780
        nh = min(20000, 420 + n_clusters * slot)
        ext_xml = ext_xml.replace('pageHeight="2970"', f'pageHeight="{nh}"')
        ext_xml = ext_xml.replace('height="2860"', f'height="{nh - 120}"')
    elif 'pageWidth="2400"' in ext_xml:
        slot = 880
        nh = min(12000, 320 + n_clusters * slot)
        ext_xml = ext_xml.replace('pageHeight="1600"', f'pageHeight="{nh}"')
        ext_xml = ext_xml.replace('height="1500"', f'height="{nh - 140}"')
    elif 'pageWidth="827"' in ext_xml:
        slot = 300
        nh = min(4000, 100 + n_clusters * slot)
        ext_xml = ext_xml.replace('pageHeight="1169"', f'pageHeight="{nh}"')
        ext_xml = ext_xml.replace('height="1100"', f'height="{nh - 40}"')
    return ext_xml


def _patch_k8s_items_for_one_page(items):
    """На одной странице корневые кластеры — вертикальная стопка (см. algo Y_stack)."""
    if not _k8s_on_one_page:
        return items
    out = []
    for k, v in items:
        if k in ('k8s_cluster', 'k8s_cluster_minimal', 'k8s_cluster_middle'):
            v = deepcopy(v)
            v['algo'] = 'Y_stack'
            v['offset'] = max(int(v.get('offset') or 0), 24)
        out.append((k, v))
    return out


def _normalize_k8s_diagram_detail(raw) -> str:
    v = str(raw or 'full').strip().lower()
    return v if v in ('minimal', 'middle', 'full') else 'full'


def _k8s_pattern_applies(pattern_key, pattern, detail: str) -> bool:
    dm = pattern.get('diagram_modes')
    if dm is not None:
        allowed = {_normalize_k8s_diagram_detail(x) for x in dm if x is not None}
        return detail in allowed
    if pattern.get('ext_page') and not pattern.get('xml'):
        return detail != 'minimal'
    return detail != 'minimal'


def _init_k8s_runtime_context():
    """Читает k8s.yaml, выставляет глобали; возвращает (detail, items | None).
    items=None, если в данных нет seaf.company.ta.services.k8s — страницы K8s не создаются.
    """
    global _k8s_diagram_details, _k8s_hpa_by_target, _k8s_clusters_with_hpa
    global _k8s_worker_count_by_cluster, _k8s_deployment_namespace
    global _k8s_on_one_page, _k8s_unified_page_title
    raw = d.read_yaml_file(patterns_dir + 'k8s.yaml') or {}
    detail = _normalize_k8s_diagram_detail(raw.get('Diagram details', 'full'))
    _k8s_on_one_page = _normalize_k8s_yes_no(raw.get('On one page', 'no'))
    _k8s_unified_page_title = str(raw.get('On one page title', 'Kubernetes (все кластеры)')).strip() \
        or 'Kubernetes (все кластеры)'
    items = [(k, v) for k, v in raw.items()
             if k not in _K8S_YAML_META_KEYS and isinstance(v, dict)]
    _k8s_diagram_details = detail
    merged = d.read_and_merge_yaml(conf['data_yaml_file'])
    k8s_clusters = merged.get('seaf.company.ta.services.k8s') or {}
    if not k8s_clusters:
        _k8s_hpa_by_target = {}
        _k8s_clusters_with_hpa = set()
        _k8s_worker_count_by_cluster = {}
        _k8s_deployment_namespace = {}
        return detail, None
    hpas = merged.get('seaf.company.ta.components.k8s_hpa') or {}
    _k8s_hpa_by_target = {}
    _k8s_clusters_with_hpa = set()
    for row in hpas.values():
        if not isinstance(row, dict):
            continue
        t = row.get('target')
        if t:
            _k8s_hpa_by_target[str(t)] = row
        c = row.get('cluster')
        if c:
            _k8s_clusters_with_hpa.add(str(c))
    nodes = merged.get('seaf.company.ta.components.k8s_nodes') or {}
    _k8s_worker_count_by_cluster = {}
    for nrow in nodes.values():
        if not isinstance(nrow, dict):
            continue
        c = nrow.get('cluster')
        if not c:
            continue
        for lab in (nrow.get('labels') or []):
            if 'worker=true' in str(lab):
                cs = str(c)
                _k8s_worker_count_by_cluster[cs] = _k8s_worker_count_by_cluster.get(cs, 0) + 1
                break
    deps = merged.get('seaf.company.ta.services.k8s_deployments') or {}
    _k8s_deployment_namespace = {}
    for did, drow in deps.items():
        if isinstance(drow, dict) and drow.get('namespace'):
            _k8s_deployment_namespace[str(did)] = str(drow['namespace'])
    items = _patch_k8s_items_for_one_page(items)
    return detail, items


def _infer_k8s_version_line(data: dict) -> str:
    for s in (data.get('softwares') or []):
        m = re.search(r'k8s[_\s]*(\d+)[_.](\d+)', str(s), re.I)
        if m:
            return f"{m.group(1)}.{m.group(2)}"
    return '—'


def _short_seaf_tokens(values, max_items: int = 4, per_token_len: int = 24, *, last_segments: int = 1) -> str:
    """Короткие подписи из списка SEAF-id (последний сегмент или несколько с конца)."""
    if not values:
        return '—'
    if not isinstance(values, list):
        values = [values]
    out = []
    for x in values[:max_items]:
        if x in (None, ''):
            continue
        parts = str(x).split('.')
        if last_segments > 1 and len(parts) >= last_segments:
            s = '.'.join(parts[-last_segments:]).replace('_', ' ')
        else:
            s = parts[-1].replace('_', ' ')
        if len(s) > per_token_len:
            s = s[: per_token_len - 1] + '…'
        out.append(s)
    return ', '.join(out) if out else '—'


def _k8s_xml_escape(s) -> str:
    """Безопасная подстановка в DrawIO label (XML-атрибут / разметка)."""
    t = '' if s is None else str(s)
    return (
        t.replace('&', '&amp;')
        .replace('<', '&lt;')
        .replace('>', '&gt;')
        .replace('"', '&quot;')
    )


def _enrich_k8s_middle_row(shape_schema: str, key_id: str, data: dict) -> None:
    """Поля разметки для Diagram details: middle (как Middle K8S_Р41 v02)."""
    if not isinstance(data, dict):
        return
    kid = str(key_id)
    if shape_schema == 'seaf.company.ta.services.k8s':
        data.setdefault('cluster', kid)
        _enrich_k8s_minimal_row(shape_schema, key_id, data)
        regs = (data.get('registries') or [])
        mon = (data.get('monitoring') or [])
        nets = (data.get('network_connection') or []) + (data.get('management_networks') or [])
        mesh = data.get('service_mesh') or '—'
        data['_mid_registries'] = _k8s_xml_escape(_short_seaf_tokens(regs, 5))
        data['_mid_monitoring'] = _k8s_xml_escape(_short_seaf_tokens(mon, 5))
        data['_mid_networks'] = _k8s_xml_escape(_short_seaf_tokens(nets, 4, last_segments=2))
        data['_mid_mesh'] = _k8s_xml_escape(str(mesh) if mesh not in (None, '') else '—')
    elif shape_schema == 'seaf.company.ta.components.k8s_namespaces':
        _enrich_k8s_minimal_row(shape_schema, key_id, data)
        desc = data.get('description') or ''
        if len(desc) > 120:
            desc = desc[: 119] + '…'
        data['_mid_ns_description'] = _k8s_xml_escape(desc) or '—'
        data['_mid_ns_key'] = _k8s_xml_escape(kid.split('.')[-1].replace('_', '.'))
    elif shape_schema == 'seaf.company.ta.services.k8s_deployments':
        containers = data.get('containers') or []
        c0 = containers[0] if containers else {}
        image = str(c0.get('image') or '—')
        if len(image) > 42:
            image = image[: 41] + '…'
        lim = (c0.get('resources') or {}).get('limits') or {}
        cpu = lim.get('cpu', '—')
        ram = lim.get('ram', lim.get('memory', '—'))
        labels = data.get('labels') or []
        label0 = str(labels[0]) if isinstance(labels, list) and labels else '—'
        dep_short = kid.split('.')[-1].replace('_', '-')
        data['_mid_dep_short'] = _k8s_xml_escape(dep_short)
        data['_mid_dep_image'] = _k8s_xml_escape(image)
        data['_mid_dep_cpu'] = _k8s_xml_escape(cpu)
        data['_mid_dep_ram'] = _k8s_xml_escape(ram)
        data['_mid_dep_label0'] = _k8s_xml_escape(label0)
        apps = data.get('app_components') or []
        app0 = str(apps[0]).split('.')[-1] if isinstance(apps, list) and apps else dep_short
        data['_mid_app_short'] = _k8s_xml_escape(app0.replace('_', '.'))
    elif shape_schema == 'seaf.company.ta.components.k8s_hpa':
        _enrich_k8s_minimal_row(shape_schema, key_id, data)
    elif shape_schema == 'seaf.company.ta.components.k8s_nodes':
        arch = str(data.get('architecture') or 'amd64')
        ver = str(data.get('version') or '—')
        zone = str(data.get('zone') or '—')
        zshort = zone.split('.')[-1] if zone else '—'
        labels = data.get('labels') or []
        lab_text = ' '.join(str(x) for x in labels) if isinstance(labels, list) else ''
        acc = None
        for lb in (labels if isinstance(labels, list) else []):
            s = str(lb).lower()
            if 'accelerator=' in s or 'nvidia' in s or 'gpu' in s:
                acc = str(lb).split('=')[-1] if '=' in str(lb) else str(lb)
                break
        data['_mid_node_arch'] = arch
        data['_mid_node_ver'] = ver
        data['_mid_node_zone_short'] = zshort
        data['_mid_node_accel_line'] = ''
        if acc:
            data['_mid_node_accel_line'] = f'&lt;br/&gt;accelerator={acc}'
        is_worker = 'worker=true' in lab_text
        data['_mid_node_is_gpu_worker'] = bool(
            is_worker and ('nvidia' in lab_text.lower() or 'accelerator=' in lab_text.lower() or 'gpu' in lab_text.lower()))


def _enrich_k8s_minimal_row(shape_schema: str, key_id: str, data: dict) -> None:
    if not isinstance(data, dict):
        return
    kid = str(key_id)
    if shape_schema == 'seaf.company.ta.services.k8s':
        data['_k8s_ver'] = _infer_k8s_version_line(data)
        data['_cni_product'] = (data.get('cni') or {}).get('product', '—')
        data['_autoscaler_on'] = 'ON' if data.get('cluster_autoscaler') else 'OFF'
        st = data.get('stand')
        if isinstance(st, list) and st:
            data['_stand_short'] = str(st[0]).split('.')[-1]
        else:
            data['_stand_short'] = '—'
        data['_hpa_yes'] = 'YES' if kid in _k8s_clusters_with_hpa else 'NO'
        if data.get('service_mesh') in (None, ''):
            data['service_mesh'] = '—'
        lk = kid.lower()
        is_llm = 'llm' in lk
        data['_k8s_minimal_fill'] = '#e2f0d9' if is_llm else '#dae8fc'
        data['_k8s_minimal_stroke'] = '#548235' if is_llm else '#6c8ebf'
        data['_k8s_minimal_fs'] = '11' if is_llm else '9'
        data['_k8s_minimal_start'] = '60' if is_llm else '50'
        au = data['_autoscaler_on']
        if is_llm:
            data['_k8s_minimal_row2'] = (
                f"Autoscaler: {au} | GPU Nodes | Stand: {data['_stand_short']}")
        else:
            data['_k8s_minimal_row2'] = (
                f"Autoscaler: {au} | HPA: {data['_hpa_yes']} | Stand: {data['_stand_short']}")
    elif shape_schema == 'seaf.company.ta.services.k8s_deployments':
        hpa = _k8s_hpa_by_target.get(kid, {})
        pods = hpa.get('min', 2)
        try:
            pods = int(pods)
        except (TypeError, ValueError):
            pods = 2
        containers = data.get('containers') or []
        c0 = containers[0] if containers else {}
        pname = (c0.get('name') or kid.split('.')[-1]).replace('_', '-')
        data['_pod_line'] = f"{pname}-pod (×{pods})"
        cl = str(data.get('cluster') or '')
        wn = _k8s_worker_count_by_cluster.get(cl, 1)
        data['_worker_line'] = f"worker (×{wn})"
    elif shape_schema == 'seaf.company.ta.components.k8s_hpa':
        tgt = str(data.get('target') or '')
        data['_hpa_target_short'] = tgt.split('.')[-1] if tgt else '—'
        ns = _k8s_deployment_namespace.get(tgt, '')
        if ns:
            data['namespace'] = ns
    elif shape_schema == 'seaf.company.ta.components.k8s_namespaces':
        lk = str(data.get('cluster') or '').lower()
        data['_k8s_ns_fs'] = '11' if 'llm' in lk else '9'
        labels = data.get('labels') or []
        if isinstance(labels, list) and labels:
            data['_ns_labels_line'] = ' | '.join(str(x) for x in labels[:3])
        else:
            data['_ns_labels_line'] = ''


def add_pages(pattern, pages_bucket_key: str, restore_page=None):

    if pattern.get('ext_page'):
        page_data = d.get_object(conf['data_yaml_file'], pattern['schema'])
        diagram_xml_default = diagram.drawio_diagram_xml

        if pages_bucket_key == 'k8s' and _k8s_on_one_page:
            ext_xml = _expand_k8s_ext_page_for_one_page(
                pattern['ext_page'], max(1, len(page_data)))
            diagram.drawio_diagram_xml = ext_xml
            try:
                diagram.add_diagram('k8s_unified_page', _k8s_unified_page_title)
                diagram_pages[pages_bucket_key].append(_k8s_unified_page_title)
                for key_id in list(page_data.keys()):
                    d.append_to_dict(diagram_ids, _k8s_unified_page_title, key_id)
            except ET.ParseError:
                print(f'WARNING ! Не используйте XML зарезервированные символы <>&\'\" в поле title для объектов dc/office')
                pass
            diagram.drawio_diagram_xml = diagram_xml_default
            if restore_page is not None:
                diagram.go_to_diagram(restore_page)
            return

        for key_id in list( page_data.keys() ):

            diagram.drawio_diagram_xml = pattern['ext_page']
            try:
                diagram.add_diagram(key_id + '_page', page_data[key_id]['title'])
                diagram_pages[pages_bucket_key].append(page_data[key_id]['title'])
                d.append_to_dict(diagram_ids, page_data[key_id]['title'], key_id)
            except ET.ParseError:
                print(f'WARNING ! Не используйте XML зарезервированные символы <>&\'\" в поле title для объектов dc/office')
                pass


        diagram.drawio_diagram_xml = diagram_xml_default
        if restore_page is not None:
            diagram.go_to_diagram(restore_page)

def add_object(pattern, data, key_id):

    if file_name == 'k8s' and _k8s_diagram_details == 'minimal':
        _enrich_k8s_minimal_row(pattern.get('schema') or '', key_id, data)
    elif file_name == 'k8s' and _k8s_diagram_details == 'middle':
        _enrich_k8s_middle_row(pattern.get('schema') or '', key_id, data)

    pattern_count, current_parent = 0, ''
    for xml_pattern in d.get_xml_pattern(pattern['xml'], key_id):

        diagram.drawio_node_object_xml = xml_pattern

        # Если у элемента есть родитель, получаем ID родителя и проверяем связан ли родитель с текущей диаграммой (страницей)
        # добавляем в справочник ID элемента
        if pattern.get('parent_id') and d.find_common_element(d.find_key_value(data, pattern['parent_id']),
                                                     diagram_ids[page_name]) and pattern_count == 0:

            d.append_to_dict(diagram_ids, page_name, key_id)
            current_parent = d.find_common_element(d.find_key_value(data, pattern['parent_id']),diagram_ids[page_name])

            # If parent_id field is a list (e.g., WAN.segment), normalize it to the selected current_parent
            try:
                if isinstance(data.get(pattern['parent_id']), list):
                    data['parent_tmp'] = data.get(pattern['parent_id'])
                    data[pattern['parent_id']] = current_parent
            except Exception:
                pass

            if current_parent != pattern['last_parent'] and pattern['parent_id'] != 'network_connection':
                new_pt = get_parent_value(pattern, current_parent)
                old_last = pattern['last_parent']
                old_pt = get_parent_value(pattern, old_last) if old_last else None
                default_pattern['parent'] = new_pt
                # Тип зоны (INTERNET / TRANSPORT-WAN) задаёт «колонку». Раньше pattern.update
                # сбрасывал y при каждом смене сегмента; при возврате в INTERNET после
                # TRANSPORT-WAN (другой DC) DC01 и DC02 оказывались на одних y — полное
                # перекрытие. Сохраняем курсор y по зоне и восстанавливаем при повторе.
                if not old_last:
                    pattern.update(default_pattern)
                elif new_pt == old_pt:
                    default_pattern['parent'] = new_pt
                    pattern['parent'] = new_pt
                else:
                    if old_pt == 'INTERNET' and new_pt != 'INTERNET':
                        pattern['_isp_y_internet'] = pattern.get('y', default_pattern.get('y'))
                    if old_pt == 'TRANSPORT-WAN' and new_pt != 'TRANSPORT-WAN':
                        pattern['_isp_y_transport'] = pattern.get('y', default_pattern.get('y'))
                    pattern.update(default_pattern)
                    if new_pt == 'INTERNET' and '_isp_y_internet' in pattern:
                        pattern['y'] = pattern['_isp_y_internet']
                    if new_pt == 'TRANSPORT-WAN' and '_isp_y_transport' in pattern:
                        pattern['y'] = pattern['_isp_y_transport']
                pattern['parent'] = new_pt
                pattern['last_parent'] = current_parent


        try:
            if pattern.get('node_id_suffix'):
                _draw_id = f'{key_id}{pattern["node_id_suffix"]}'
            else:
                _draw_id = key_id
            fmt_extra = {
                'Group_ID': f'{key_id}_0',
                'parent_id': current_parent,
                'parent_type': default_pattern['parent'],
                'description': data.get('description', ''),
                'id': _draw_id,
            }
            # Родитель-сегмент для шаблонов с parent="{segment}" и согласованного вложения в контейнер
            if pattern.get('parent_id') == 'segment' and current_parent:
                fmt_extra['segment'] = current_parent
            diagram.drawio_node_object_xml = diagram.drawio_node_object_xml.format_map(
                data | fmt_extra
            )
            data['OID'] = key_id

        except KeyError as e:

            #print("Error: Can't add object: {id} to page: {page}. Key: {key} out of dictionary. Data: {data}"
            #      .format(key=str(e), id=i, page=page_name, data=data))
            return


        if key_id in diagram_ids[page_name]:

            #if pattern.get('parent_id') == 'dc':
            #    print(f'==={i} == {current_parent} === {key_id}_{pattern_count}')
            """
                Заменяет ключ 'id' на 'sid' в словаре, если он существует.
            """
            if 'id' in data:
                data['sid'] = data.pop('id')

            data['schema'] = pattern['schema']

            # Удаляем техническое поле если оно присутствует в данных
            if 'parent_tmp' in data:
                del data['parent_tmp']

            # Main Schema: размеры контейнеров INTERNET / TRANSPORT-WAN по числу WAN и раскладке isp из main.yaml
            if page_name == 'Main Schema' and _is_main_schema_zone_segment(pattern):
                nw, nh = compute_main_schema_segment_dimensions(key_id, pattern)
                pattern['w'], pattern['h'] = nw, nh

            wan_override = wan_edge_layout_cache.get('positions', {}).get(key_id)
            if wan_override:
                pattern['x'] = wan_override['x']
                pattern['y'] = wan_override['y']
                pattern['w'] = wan_override['w']
                pattern['h'] = wan_override['h']
            seg_auto = wan_edge_layout_cache.get('segment_size', {}).get(key_id)
            if seg_auto and _is_segment_auto_size_from_layout(pattern):
                pattern['w'], pattern['h'] = seg_auto['w'], seg_auto['h']
            seg_origin = wan_edge_layout_cache.get('segment_origin', {}).get(key_id)
            if seg_origin:
                if 'x' in seg_origin:
                    pattern['x'] = seg_origin['x']
                if 'y' in seg_origin:
                    pattern['y'] = seg_origin['y']

            draw_node_id = (
                f"{key_id}_{pattern_count}" if not d.contains_object_tag(xml_pattern, 'object')
                else (f"{key_id}{pattern['node_id_suffix']}" if pattern.get('node_id_suffix') else key_id)
            )
            diagram.add_node(
                id=draw_node_id,
                label=data['title'],
                x_pos=pattern['x'],
                y_pos=pattern['y'],
                width=pattern['w'],
                height=pattern['h'],
                data=data if d.contains_object_tag(xml_pattern, 'object') else {},
                url=pattern.get('ext_page') and data['title']
            )
            d.append_to_dict(diagram_ids, page_name, key_id)  # Добавляет ID root элементов

            if pattern_count == 0 and key_id not in wan_edge_layout_cache.get('positions', {}) \
                    and key_id not in wan_edge_layout_cache.get('segment_origin', {}):  # Change position of element
                position_offset(pattern)
            pattern_count += 1

        diagram.drawio_node_object_xml = node_xml_default

def add_links(pattern,  **kwargs):

    diagram.drawio_link_object_xml = pattern['xml']
    source_id = 'Unknown'

    for source_id, targets in d.get_object(conf['data_yaml_file'], pattern['schema'],
                                           type=pattern.get('type')).items():  # source_id - ID объекта

        if kwargs.get('logical_link'):
            targets['OID'] = source_id
            source_id = targets['source']
            targets['schema'] = pattern['schema']

        try:
            if source_id in diagram_ids[page_name]:  # Объект присутствует на текущей диаграмме
                if pattern.get('parent_id'):
                    # parent_id may be a list (e.g., WAN.segment). Derive targets for each parent entry.
                    # Prefer explicit list on the object (e.g. network.location) when pattern targets match.
                    tkey = pattern.get('targets')
                    derived_targets = []
                    if tkey and tkey == 'location' and targets.get('location') is not None:
                        loc = targets.get('location')
                        if isinstance(loc, list):
                            derived_targets = [x for x in loc if x is not None and x != '']
                        else:
                            if loc not in (None, ''):
                                derived_targets = [loc]
                    if not derived_targets:
                        parent_val = targets.get(pattern['parent_id'])
                        parent_ids = parent_val if isinstance(parent_val, list) else ([parent_val] if parent_val else [])
                        for pid in parent_ids:
                            val = get_parent_value(pattern, pid)
                            if isinstance(val, list):
                                derived_targets.extend(val)
                            elif val is not None:
                                derived_targets.append(val)
                    targets = {pattern['targets']: derived_targets}
                for target_id in targets[pattern['targets']]:
                    if target_id in diagram_ids[page_name]:  # Объект для связи присутствует на диаграмме
                        if kwargs.get('logical_link'):
                            style = 'style'+ str(targets['direction']) # Выбор стиля стрелки
                            diagram.add_link(source=source_id, target=target_id, style=pattern[style], data=targets)
                        else:
                            diagram.add_link(source=source_id, target=target_id, style=pattern['style'])
                    else:
                        # Defer logging: cross-page targets are expected; warn later only if missing everywhere
                        pending_missing_links.add((page_name, source_id, target_id))
                        #print(f' Can\'t link  {source_id} <---> {target_id}, object {target_id} not found at the page '
                        #      f'{page_name}')
        except KeyError as e:
            pass
            print(f" INFO : Не найден параметр {e} для объекта '{pattern['schema']}/{source_id}' при добавлении связей на диаграмму '{page_name}'.")
        except TypeError as e:
            pass
            print(
                f"Error: у объекта '{source_id}' отсутствует данные для создания линка в параметре {pattern['targets']} ")

def collect_ids():
    try:
        schema_key = object_pattern['schema']
        expected_counts.setdefault(schema_key, set()).update(list(object_data.keys()))
        expected_data.setdefault(schema_key, {}).update(object_data)
        # Record pattern spec for diagnostics
        type_key, type_val = None, None
        if object_pattern.get('type'):
            if ':' in object_pattern['type']:
                type_key, type_val = object_pattern['type'].split(':', 1)
            else:
                type_key, type_val = 'type', object_pattern['type']
        pattern_specs.setdefault(schema_key, []).append({
            'pattern_name': k,
            'parent_id': object_pattern.get('parent_id'),
            'type_key': type_key,
            'type_val': type_val,
        })
    except Exception as Ex:
        print(f"Exception Collect ID : {Ex}")


if __name__ == '__main__':

    if sys.version_info < (3, 9):
        print("Этот скрипт требует Python версии 3.9 или выше.")
        sys.exit(1)

    conf = cli_vars(d.load_config("config.yaml")['seaf2drawio'])
    _main_isp_layout_cache = None

    diagram.from_xml(d.read_file_with_utf8(conf['drawio_pattern']))
    
    # Удаляем устаревшие связи перед добавлением новых
    remove_obsolete_links(diagram, conf['data_yaml_file'], 'seaf.ta.components.network')
    
    diagram_ids['Main Schema'] = list(d.get_object(conf['data_yaml_file'], root_object).keys())
    for file_name, pages in diagram_pages.items():
        k8s_pattern_items = None
        k8s_detail_level = 'full'
        if file_name == 'k8s':
            k8s_detail_level, k8s_pattern_items = _init_k8s_runtime_context()
            if k8s_pattern_items is None:
                continue
            if not pages:
                for _pk, _op in k8s_pattern_items:
                    if _op.get('ext_page') and not _op.get('xml') \
                            and _k8s_pattern_applies(_pk, _op, k8s_detail_level):
                        add_pages(_op, 'k8s', None)
                        break
                pages = diagram_pages['k8s']

        for page_name in pages:

            diagram.go_to_diagram(page_name)
            _py = os.path.join(patterns_dir, file_name + '.yaml')
            _roots = diagram_ids.get(page_name) or []
            if not _roots:
                _roots = resolve_page_location_roots(d, conf, page_name, _py)
            wan_edge_layout_cache = compute_wan_edge_layout(d, conf, page_name, _roots, _py)
            wan_edge_layout_cache.setdefault('segment_origin', {})
            _dmz_layout = compute_dmz_layout(d, conf, page_name, _roots, _py, wan_edge_layout_cache)
            wan_edge_layout_cache['positions'].update(_dmz_layout.get('positions', {}))
            wan_edge_layout_cache['segment_size'].update(_dmz_layout.get('segment_size', {}))
            wan_edge_layout_cache['segment_origin'].update(_dmz_layout.get('segment_origin', {}))
            wan_edge_layout_cache['cross_segment_firewall_oids'] = _dmz_layout.get('cross_segment_firewall_oids') or frozenset()
            align_int_net_security_bottom_to_int_wan_edge(
                d,
                conf,
                _roots,
                _py,
                wan_edge_layout_cache['segment_size'],
                wan_edge_layout_cache.get('segment_origin'),
            )
            print(f"\n> Формирую диаграмму страницы \033[32m{page_name}\033[0m ", end='')
            _pattern_iter = (
                k8s_pattern_items if file_name == 'k8s'
                else d.read_yaml_file(patterns_dir + file_name + '.yaml').items()
            )
            for k, object_pattern in _pattern_iter:
                if file_name == 'k8s' and not _k8s_pattern_applies(k, object_pattern, k8s_detail_level):
                    continue
                if object_pattern.get('ext_page') and not object_pattern.get('xml'):
                    continue
                print('.', end='')
                try:
                    object_data = d.get_object(
                        conf['data_yaml_file'],
                        object_pattern['schema'],
                        type=object_pattern.get('type'),
                        sort=(object_pattern['sort'] if 'sort' in object_pattern else
                              (object_pattern['parent_id'] if object_pattern.get('parent_id') else None)),
                        require_fields=object_pattern.get('require_fields'),
                        exclude_fields=object_pattern.get('exclude_fields'),
                    )

                    add_pages(object_pattern, k if k in diagram_pages else file_name, page_name)
                    object_pattern.update({
                                'count': 0,               # Счетчик объектов
                                'last_parent': '',        # Триггер для отслеживания изменения родительского объекта
                                'parent': ''              # Родительский объект
                    })
                    for _pop_k in [x for x in list(object_pattern.keys()) if str(x).startswith('_isp_y_')]:
                        object_pattern.pop(_pop_k, None)
                    default_pattern = deepcopy(object_pattern)

                    # Collect expected IDs and data per schema (for verification)
                    collect_ids()

                    for i in list(object_data.keys()):
                        if (i in diagram.nodes_ids[diagram.current_diagram_id]
                                and not object_pattern.get('node_id_suffix')):
                            diagram.update_node(id=i, data=object_data[i])
                            d.append_to_dict(diagram_ids, page_name, i)
                        else:
                            add_object(object_pattern, object_data[i], i)

                except KeyError as e:
                    pass
                    print(f' INFO : В файле данных отсутствуют объекты {object_pattern["schema"]} для добавления на диаграмму {page_name}')

                if bool(re.match(r'^network_links(_\d+)*',k)):
                    add_links(object_pattern, pattern_name=k)  # Связывание объектов на текущей диаграмме

                if bool(re.match(r'^logical_links(_\d+)*', k)):
                    add_links(object_pattern, logical_link=True)  # Связывание объектов на текущей диаграмме

            reorder_inet_ext_wan_edge_before_dmz_swimlane(diagram, page_name)
            bring_cross_segment_firewalls_to_front(
                diagram, page_name, wan_edge_layout_cache.get('cross_segment_firewall_oids') or frozenset(),
            )
            resize_location_label_to_cover_segments(
                diagram, page_name, file_name, diagram_ids, conf, d,
            )
            if file_name in ('dc', 'office'):
                kb_layout(diagram, d, conf, page_name, diagram_ids, _py, _roots)
                services_TA_layout(diagram, d, conf, page_name, diagram_ids, _py, _roots)

    print('\n')
    # Verifying drawn links & objects ...
    draw_verify(diagram_ids, diagram, pending_missing_links)

    d.dump_file(filename=os.path.basename(conf['output_file']), folder=os.path.dirname(conf['output_file']),
                content=diagram.drawing if os.path.dirname(conf['output_file']) else './')

    # Check additional result info ...
    advanced_analysis(conf, expected_counts, expected_data, pattern_specs, d)