from N2G import drawio_diagram
import sys
import json
import re
import os
import argparse
from copy import deepcopy
from lib import seaf_drawio
from lib.link_manager import remove_obsolete_links, draw_verify, advanced_analysis
from auto_layout.edge_segments_layout import (
    edge_segments_layout as compute_wan_edge_layout,
    resolve_page_location_roots,
)
from auto_layout.dmz_segments_layout import dmz_segments_layout as compute_dmz_layout
import xml.etree.ElementTree as ET

patterns_dir = 'data/patterns/'
_main_isp_layout_cache = None
diagram = drawio_diagram()
node_xml_default = diagram.drawio_node_object_xml
# Ключ схемы в объединённых YAML (см. data/example/dc_region.yaml)
root_object = 'seaf.company.ta.services.dc_regions'
diagram_pages = {'main': ['Main Schema'], 'office': [], 'dc': []}
diagram_ids = {'Main Schema': []}
conf = {}
pending_missing_links = set()
layout_counters = {}
expected_counts = {}
expected_data = {}
pattern_specs = {}
wan_edge_layout_cache = {'positions': {}, 'segment_size': {}, 'segment_origin': {}}

# Переменные по умолчанию
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


def add_pages(pattern):

    if pattern.get('ext_page'):
        page_data = d.get_object(conf['data_yaml_file'], pattern['schema'])
        diagram_xml_default = diagram.drawio_diagram_xml

        for key_id in list( page_data.keys() ):

            diagram.drawio_diagram_xml = pattern['ext_page']
            try:
                diagram.add_diagram(key_id + '_page', page_data[key_id]['title'])
                diagram_pages[k].append(page_data[key_id]['title'])
                d.append_to_dict(diagram_ids, page_data[key_id]['title'], key_id)
            except ET.ParseError:
                print(f'WARNING ! Не используйте XML зарезервированные символы <>&\'\" в поле title для объектов dc/office')
                pass


        diagram.drawio_diagram_xml = diagram_xml_default
        diagram.go_to_diagram(page_name)

def add_object(pattern, data, key_id):

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
            fmt_extra = {
                'Group_ID': f'{key_id}_0',
                'parent_id': current_parent,
                'parent_type': default_pattern['parent'],
                'description': data.get('description', ''),
                'id': key_id,
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

            # Если не содержит конструкции <object></object>, то изменять ID добавляя порядковый номер
            diagram.add_node(
                id=f"{key_id}_{pattern_count}" if not d.contains_object_tag(xml_pattern, 'object') else key_id,
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
            print(f"\n> Формирую диаграмму страницы \033[32m{page_name}\033[0m ", end='')
            for k, object_pattern in d.read_yaml_file(patterns_dir + file_name + '.yaml').items():
                print('.', end='')
                try:
                    object_data = d.get_object(
                        conf['data_yaml_file'],
                        object_pattern['schema'],
                        type=object_pattern.get('type'),
                        sort=object_pattern['parent_id'] if object_pattern.get('parent_id') else None,
                        require_fields=object_pattern.get('require_fields'),
                        exclude_fields=object_pattern.get('exclude_fields'),
                    )

                    add_pages(object_pattern)
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
                        if i in diagram.nodes_ids[diagram.current_diagram_id]:
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

    print('\n')
    # Verifying drawn links & objects ...
    draw_verify(diagram_ids, diagram, pending_missing_links)

    d.dump_file(filename=os.path.basename(conf['output_file']), folder=os.path.dirname(conf['output_file']),
                content=diagram.drawing if os.path.dirname(conf['output_file']) else './')

    # Check additional result info ...
    advanced_analysis(conf, expected_counts, expected_data, pattern_specs, d)