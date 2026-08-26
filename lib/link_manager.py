import xml.etree.ElementTree as ET

def find_parent(root, target):
    """Находит родительский элемент для target в дереве root"""
    for elem in root.iter():
        for child in list(elem):
            if child is target:
                return elem
    return None

def collect_data_links(object_data):
    """
    Собирает все связи из данных объекта.
    Проверяет различные возможные поля для связей.
    """
    data_links = set()

    for source_id, targets in object_data.items():
        # Проверяем разные возможные ключи для связей
        possible_keys = ['location', 'network_connection', 'connections', 'links']

        for key in possible_keys:
            if key in targets:
                connections = targets[key]
                # Убедимся, что это список
                if not isinstance(connections, list):
                    connections = [connections]

                for target_id in connections:
                    # Используем кортеж (source, target) как ключ
                    # Учитываем, что связи двунаправленные
                    link_key = tuple(sorted([source_id, target_id]))
                    data_links.add(link_key)

    return data_links

def remove_obsolete_links(diagram, data_file, schema_key):
    """
    Удаляет связи из диаграммы, которые отсутствуют в новых данных.
    
    :param diagram: Экземпляр drawio_diagram
    :param data_file: Путь к YAML-файлу с данными
    :param schema_key: Ключ схемы для поиска связей в данных
    """
    # Получаем связи из новых данных
    from lib import seaf_drawio

    d = seaf_drawio.SeafDrawio({})
    object_data = d.get_object(data_file, schema_key)

    # Собираем связи из данных и список ID компонентов
    data_links = collect_data_links(object_data)
    component_ids = set(object_data.keys())

    # Получаем все связи из диаграммы
    existing_links = {}

    # Работаем напрямую с XML-деревом диаграммы
    root = diagram.drawing

    # Создаем отображение ID вершин mxCell в ID объектов
    cell_to_object = {}
    for obj in root.iter('object'):
        cell = obj.find('mxCell')
        if cell is not None and cell.get('vertex') == '1':
            cell_to_object[cell.get('id')] = obj.get('id')

    # Ищем все элементы object с атрибутом edge (связи)
    for obj in root.iter('object'):
        cell = obj.find('mxCell')
        if cell is not None and cell.get('edge') == '1':
            source = cell_to_object.get(cell.get('source'), cell.get('source'))
            target = cell_to_object.get(cell.get('target'), cell.get('target'))
            if (
                    source and target
                    and source in component_ids
                    and ('.lan.' in target or '.wan.' in target)
            ):
                # Используем кортеж (source, target) как ключ
                # Учитываем, что связи двунаправленные
                link_key = tuple(sorted([source, target]))
                existing_links[link_key] = obj.get('id')

    # Определяем связи для удаления
    links_to_remove = set(existing_links.keys()) - data_links

    # Выводим информацию о связях, которые будут удалены
    if links_to_remove:
        print(f"Удаляем {len(links_to_remove)} устаревших связей:")
        for link_key in links_to_remove:
            print(f"  {link_key[0]} -> {link_key[1]}")
    else:
        print("Нет устаревших связей для удаления.")

    # Удаляем устаревшие связи напрямую из XML
    for link_key in links_to_remove:
        link_id = existing_links[link_key]
        # Находим и удаляем элемент object с соответствующим ID
        for obj in root.findall('.//object[@id="{}"]'.format(link_id)):
            # Находим родителя и удаляем из него
            parent = find_parent(root, obj)
            if parent is not None:
                parent.remove(obj)

def draw_verify(diagram_ids, diagram, pending_missing_links):


    # After constructing all pages, log only truly missing targets (not present on any page)
    try:
        present_ids = set()
        for ids in diagram_ids.values():
            present_ids.update(ids)
        real_missing = sorted({(p, s, t) for (p, s, t) in pending_missing_links if t not in present_ids})
        if real_missing:
            print(f"INFO: skipped {len(real_missing)} links due to targets missing on all pages:")
            for p_name, source_id, target_id in real_missing:
                print(f"  {p_name}: {source_id} -> {target_id}")
    except Exception as Ex:
        print(f"\033[91mLinks Verify Exception \033[0m:\n {Ex}")

    # Post-process: distribute KB services vertically per x column (parent=101) to avoid overlaps
    try:
        root = diagram.drawing
        for draw in root.findall('.//diagram'):
            kb_cells = []
            for c in draw.iter('mxCell'):
                if c.get('vertex') == '1' and c.get('parent') == '101':
                    geo = c.find('mxGeometry')
                    if geo is not None and geo.get('x') is not None and geo.get('y') is not None:
                        try:
                            x = int(float(geo.get('x')))
                            y = int(float(geo.get('y')))
                        except ValueError:
                            continue
                        kb_cells.append((c, x, y))
            from collections import defaultdict
            groups = defaultdict(list)
            for c, x, y in kb_cells:
                groups[x].append((c, y))
            for x, items in groups.items():
                items.sort(key=lambda t: (t[1], t[0].get('id')))
                if not items:
                    continue
                y0 = min(y for _, y in items)
                step = 70  # height 40 + offset 30 from KB patterns
                for idx, (c, _) in enumerate(items):
                    geo = c.find('mxGeometry')
                    if geo is not None:
                        geo.set('y', str(y0 + idx * step))
    except Exception as Ex:
        print(f"Draw Verify Exception :\n {Ex}")

def advanced_analysis(conf, expected_counts, expected_data, pattern_specs, d):

    ignore_object_ids = {"981", "991"}

    # Optional verification summary against final generated file
    try:
        if conf.get('verify_generation'):
            final_path = conf['output_file']
            tree = ET.parse(final_path)
            root_xml = tree.getroot()

            # Gather drawn counts per schema (global and per page)
            drawn_unique = {}
            drawn_total = {}
            per_page_unique = {}
            per_page_total = {}
            # per-page (iterate diagrams)
            for diag in root_xml.findall('.//diagram'):
                page = diag.get('name') or 'Unknown'
                per_page_unique.setdefault(page, {})
                per_page_total.setdefault(page, {})
                for obj in diag.iter('object'):
                    schema = obj.get('schema')
                    oid = obj.get('id')
                    if not schema or not oid:
                        continue
                    oid = obj.get('source_oid') or oid
                    if oid in ignore_object_ids:
                        continue
                    # Logical links: use OID (semantic id) if available, skip non-edge objects
                    if schema == 'seaf.ta.services.logical_link':
                        cell = obj.find('mxCell')
                        if cell is None or cell.get('edge') != '1':
                            continue
                        oid = obj.get('OID') or oid
                    per_page_total[page][schema] = per_page_total[page].get(schema, 0) + 1
                    per_page_unique[page].setdefault(schema, set()).add(oid)
            for obj in root_xml.findall('.//object'):
                schema = obj.get('schema')
                oid = obj.get('id')
                if not schema or not oid:
                    continue
                oid = obj.get('source_oid') or oid
                if oid in ignore_object_ids:
                    continue
                if schema == 'seaf.ta.services.logical_link':
                    cell = obj.find('mxCell')
                    if cell is None or cell.get('edge') != '1':
                        continue
                    oid = obj.get('OID') or oid
                drawn_total[schema] = drawn_total.get(schema, 0) + 1
                drawn_unique.setdefault(schema, set()).add(oid)

            # Print summary per schema based on expected_counts gathered from patterns
            schemas = sorted(set(list(expected_counts.keys()) + list(drawn_unique.keys())))
            all_match = True
            print("--- Verification summary (by schema):\n")
            for schema in schemas:
                exp = len(expected_counts.get(schema, set()))
                drw_u = len(drawn_unique.get(schema, set()))
                drw_t = drawn_total.get(schema, 0)
                match = (exp == drw_u)
                all_match = all_match and match
                GREEN = '\033[92m'
                RED = '\033[91m'
                RESET = '\033[0m'

                status = 'OK' if match else 'MISMATCH'
                color = GREEN if match else RED

                print(
                    f"  - {schema}: expected={exp}, drawn_unique={drw_u}, drawn_total={drw_t} -> {color}{status}{RESET}"
                )

            if not all_match:
                # Show a small diff preview
                for schema in schemas:
                    exp_set = expected_counts.get(schema, set())
                    drw_set = drawn_unique.get(schema, set())
                    missing = list(exp_set - drw_set)[:5]
                    extra = list(drw_set - exp_set)[:5]
                    if missing or extra:
                        if missing:
                            print(f"    missing in diagram ({schema}): {missing}...")
                        if extra:
                            print(f"    extra in diagram ({schema}): {extra}...")

                # Detailed diagnostics for missing items
                all_oids = set()
                for obj in root_xml.findall('.//object'):
                    if obj.get('id'):
                        all_oids.add(obj.get('id'))
                print("\n--- Diagnostics for missing items:\n")
                # Pre-compute expected values per schema/type_key for concise messages
                schema_expected = {}
                for schema in schemas:
                    specs = pattern_specs.get(schema, [])
                    if not specs:
                        continue
                    # choose most common type_key, collect all its expected values
                    key_counts = {}
                    values_by_key = {}
                    for spec in specs:
                        tk, tv = spec.get('type_key'), spec.get('type_val')
                        if not tk or not tv:
                            continue
                        key_counts[tk] = key_counts.get(tk, 0) + 1
                        values_by_key.setdefault(tk, set()).add(tv)
                    if values_by_key:
                        best_key = max(key_counts.items(), key=lambda x: x[1])[0]
                        schema_expected[schema] = (best_key, values_by_key.get(best_key, set()))

                for schema in schemas:
                    exp_set = expected_counts.get(schema, set())
                    drw_set = drawn_unique.get(schema, set())
                    missing_ids = list(exp_set - drw_set)
                    if not missing_ids:
                        continue
                    print(f"  {schema}:")
                    tkey, tvals = schema_expected.get(schema, (None, set()))
                    for mid in missing_ids[:10]:
                        data = expected_data.get(schema, {}).get(mid, {})
                        msg_parts = []
                        if tkey:
                            vals = d.find_key_value(data, tkey)
                            actual = vals[0] if isinstance(vals, list) and vals else None
                            # Prepare expected list (limited)
                            ev = sorted(v for v in tvals)
                            ev_out = ", ".join(ev[:6]) + (" ..." if len(ev) > 6 else "")
                            msg_parts.append(f"{tkey}='{actual}' | expected: {ev_out}")
                        # parent_id hint (first parent spec)
                        pid = None
                        for spec in pattern_specs.get(schema, []):
                            if spec.get('parent_id'):
                                pid = spec.get('parent_id')
                                break
                        if pid:
                            parents = d.find_key_value(data, pid)
                            present = any(p in all_oids for p in (parents if isinstance(parents, list) else [parents]))
                            if not present:
                                msg_parts.append(f"parent '{pid}' not present on pages")
                        print(f"    - {mid}: " + ("; ".join(msg_parts) if msg_parts else "no rule matched"))

            # Per-page breakdown (drawn counts)
            print("\nPer-page summary (drawn, by schema):")
            for page in sorted(per_page_total.keys()):
                print(f"  Page: {page}")
                schemas_p = sorted(set(list(per_page_total[page].keys()) + list(per_page_unique[page].keys())))
                for schema in schemas_p:
                    du = len(per_page_unique[page].get(schema, set()))
                    dt = per_page_total[page].get(schema, 0)
                    print(f"    - {schema}: drawn_unique={du}, drawn_total={dt}")

            print("Result:",
                  "GENERATION MATCHES YAML (by schema)" if all_match else "GENERATION DIFFERS FROM YAML (by schema)")
    except Exception as e:
        print(f"\n\033[91mVerification step failed:\033[0m {e}")
