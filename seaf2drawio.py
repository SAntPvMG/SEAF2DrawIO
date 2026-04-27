from N2G import drawio_diagram
import sys
import json
import re
import os
import argparse
from copy import deepcopy
from lib import seaf_drawio
from lib.link_manager import remove_obsolete_links, draw_verify, advanced_analysis
import xml.etree.ElementTree as ET

patterns_dir = 'data/patterns/'
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

def get_parent_value(pattern, current_parent):
    r = ''
    if pattern.get('parent_key'):
        r = d.find_value_by_key(d.find_value_by_key(json.loads(json.dumps(d.read_and_merge_yaml(conf['data_yaml_file']))),
                                                    current_parent), pattern['parent_key'])
    return r

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

            if current_parent != pattern['last_parent'] and pattern['parent_id'] !='network_connection':   # reset to default pattern
                default_pattern['parent'] = get_parent_value(pattern, current_parent)
                pattern.update(default_pattern)
                pattern['last_parent'] = current_parent


        try:
            diagram.drawio_node_object_xml = diagram.drawio_node_object_xml.format_map(
                data | {'Group_ID': f'{key_id}_0', 'parent_id' : current_parent, 'parent_type' : default_pattern['parent'],
                        'description' : data.get('description','') })  # замена в xml шаблоне переменных в одинарных {}, добавление ID группы
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

            if pattern_count == 0:  # Change position of element
                position_offset(object_pattern)
            pattern_count += 1

        diagram.drawio_node_object_xml = node_xml_default

def add_links(pattern,  **kwargs):

    diagram.drawio_link_object_xml = pattern['xml']
    source_id = 'Unknown'

    for source_id, targets in d.get_object(conf['data_yaml_file'], pattern['schema'],
                                           type=object_pattern.get('type')).items():  # source_id - ID объекта

        if kwargs.get('logical_link'):
            targets['OID'] = source_id
            source_id = targets['source']
            targets['schema'] = pattern['schema']

        try:
            if source_id in diagram_ids[page_name]:  # Объект присутствует на текущей диаграмме
                if pattern.get('parent_id'):
                    # parent_id may be a list (e.g., WAN.segment). Derive targets for each parent entry.
                    parent_val = targets.get(pattern['parent_id'])
                    parent_ids = parent_val if isinstance(parent_val, list) else ([parent_val] if parent_val else [])
                    derived_targets = []
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

    diagram.from_xml(d.read_file_with_utf8(conf['drawio_pattern']))
    
    # Удаляем устаревшие связи перед добавлением новых
    remove_obsolete_links(diagram, conf['data_yaml_file'], 'seaf.ta.components.network')
    
    diagram_ids['Main Schema'] = list(d.get_object(conf['data_yaml_file'], root_object).keys())
    for file_name, pages in diagram_pages.items():

        for page_name in pages:

            diagram.go_to_diagram(page_name)
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

    print('\n')
    # Verifying drawn links & objects ...
    draw_verify(diagram_ids, diagram, pending_missing_links)

    d.dump_file(filename=os.path.basename(conf['output_file']), folder=os.path.dirname(conf['output_file']),
                content=diagram.drawing if os.path.dirname(conf['output_file']) else './')

    # Check additional result info ...
    advanced_analysis(conf, expected_counts, expected_data, pattern_specs, d)