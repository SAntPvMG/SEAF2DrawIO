# Полный алгоритм `seaf2drawio.py`: проходы и реализация

Документ дополняет [seaf2drawio-algorithm.md](seaf2drawio-algorithm.md): здесь поток выполнения разложен по **проходам** с привязкой к модулям и функциям. Целевой пост-проход LAN / User Devices см. [lan-overlay-segment-algorithm.md](lan-overlay-segment-algorithm.md) и статус внедрения — [plan-lan-overlay-segment-work.md](plan-lan-overlay-segment-work.md).

---

## Глобальный контур

1. Загрузка конфигурации (`config.yaml`, переопределения CLI через `cli_vars` в `seaf2drawio.py`).
2. Создание `drawio_diagram`, загрузка базового XML шаблона (`drawio_pattern`).
3. `remove_obsolete_links` — очистка устаревших связей.
4. Заполнение `diagram_ids['Main Schema']` из схемы `seaf.company.ta.services.dc_regions` (`root_object`).
5. Цикл по `diagram_pages`: для каждого `file_name` (`main`, `office`, `dc`, `k8s`) и каждой страницы — расчёт кэша раскладки, добавление узлов и связей из `data/patterns/{file_name}.yaml` (для `k8s` — особая инициализация и фильтрация паттернов).
6. После всех страниц: `draw_verify`, запись `.drawio`, `advanced_analysis`.

---

## Проход A — старт (один раз перед циклом страниц)

| Шаг | Действие | Где в коде |
|-----|----------|------------|
| A1 | Проверка версии Python | блок `if __name__ == '__main__'` |
| A2 | Загрузка конфига и CLI (`-s`, `-d`, `-p`) | `cli_vars`, `DEFAULT_CONFIG` |
| A3 | Загрузка диаграммы из файла | `diagram.from_xml(d.read_file_with_utf8(...))` |
| A4 | Удаление устаревших связей | `lib.link_manager.remove_obsolete_links` |
| A5 | OID объектов для Main Schema | `diagram_ids['Main Schema'] = list(d.get_object(..., root_object).keys())` |

---

## Проход B — внешний цикл по файлам паттернов

Для каждого `(file_name, pages)` из `diagram_pages`:

**Особый случай `k8s`** (до обхода страниц):

- `_init_k8s_runtime_context()` читает `data/patterns/k8s.yaml`: `Diagram details` (minimal / middle / full), `On one page`, вспомогательные структуры (HPA по target, worker-ноды по кластеру, namespace деплойментов и т.д.).
- При отсутствии страниц `add_pages` может создать страницу по паттерну с `ext_page` (в т.ч. объединённая страница при `On one page`).

Далее для каждой `page_name` выполняются проходы C–I.

---

## Проход C — подготовка страницы и корни локации

| Шаг | Действие | Где |
|-----|----------|-----|
| C1 | Переключение текущей страницы | `diagram.go_to_diagram(page_name)` |
| C2 | Путь к YAML паттернов | `_py = patterns_dir + file_name + '.yaml'` |
| C3 | Корни страницы `_roots` | `diagram_ids[page_name]` или `resolve_page_location_roots` (`edge_segments_layout.py`) |

---

## Проход D — первый слой автораскладки (WAN / intrinsic)

Заполнение `wan_edge_layout_cache`: словари `positions`, `segment_size` (начальное состояние `segment_origin` дополняется в проходе E).

| Шаг | Действие | Где |
|-----|----------|-----|
| D1 | Main Schema — пустой кэш | `edge_segments_layout`: при `page_name == 'Main Schema'` возвращаются пустые словари |
| D2 | Иначе intrinsic по набору зон | `compute_wan_edge_layout` → `compute_intrinsic_band_layout` |
| D3 | Набор зон | `WAN_EDGE_ZONES`; при `patterns_yaml_uses_interior_layout` добавляются DMZ, INT-NET, INT-SECURITY-NET (`OFFICE_INTRINSIC_ZONES` в `edge_segments_layout.py`) |
| D4 | Расчёт позиций и размеров сегментов | `segment_intrinsic_layout.compute_intrinsic_band_layout`: данные `network_segments`, `networks`, `components`; кэш по отпечатку входных данных (`layout_cache.py`) |

Итог: `wan_edge_layout_cache['positions']`, `wan_edge_layout_cache['segment_size']`.

---

## Проход E — второй слой (DMZ / сетка office и dc)

| Шаг | Действие | Где |
|-----|----------|-----|
| E1 | Вызов `compute_dmz_layout` | `dmz_segments_layout.dmz_segments_layout` |
| E2 | Interior layout | Если паттерн в режиме interior: переиспользуются `positions`/`segment_size` из D, добавляется `segment_origin` по шаблонным прямоугольникам зон и данным сегментов |
| E3 | Без interior | Отдельный `compute_intrinsic_band_layout` только для зоны DMZ |
| E4 | Слияние в общий кэш | В `seaf2drawio.py`: `update` для `positions`, `segment_size`, `segment_origin`; сохранение `cross_segment_firewall_oids` |
| E5 | Межсегментные файрволы | `apply_cross_segment_firewall_positions` внутри dmz layout |

---

## Проход F — выравнивание высот INT-NET / INT-SECURITY

| Шаг | Действие | Где |
|-----|----------|-----|
| F1 | Подстройка `segment_size` нижней границы относительно INT-WAN-EDGE | `segment_intrinsic_layout.align_int_net_security_bottom_to_int_wan_edge` |

---

## Проход G — цикл паттернов страницы

Для каждой записи `(k, object_pattern)` в паттернах страницы:

- Для `file_name == 'k8s'`: фильтр `_k8s_pattern_applies` по уровню детализации.
- Пропуск записей только с `ext_page` без `xml`.

Последовательность для паттерна:

1. `d.get_object(...)` — выборка по `schema`, `type`, `sort`, `require_fields` / `exclude_fields`.
2. `add_pages` — создание страниц по `ext_page`.
3. Сброс счётчиков паттерна; очистка временных `_isp_y_*` для Main Schema.
4. `collect_ids` — учёт ожидаемых OID для анализа.
5. Для каждого объекта: при наличии узла на диаграмме — `diagram.update_node` и дополнение `diagram_ids`; иначе — `add_object`.
6. По имени паттерна: `network_links*` → `add_links`; `logical_links*` → `add_links(..., logical_link=True)`.

---

## Проход H — внутри `add_object` (микропроход)

1. Подстановка XML шаблона (`get_xml_pattern`).
2. Разрешение `parent_id` через `diagram_ids[page_name]`.
3. Main Schema: позиции колонок ISP; при необходимости `compute_main_schema_segment_dimensions`.
4. Если OID разрешён для страницы:
   - `wan_edge_layout_cache['positions'][key_id]` → координаты и размеры из intrinsic;
   - `segment_size[key_id]` при авторазмере сегмента → размер контейнера `network_segments`;
   - `segment_origin[key_id]` → абсолютные координаты контейнера зоны;
   - `diagram.add_node`.
5. Если нет ни `positions`, ни `segment_origin` для первого фрагмента — `position_offset(pattern)` по полю `algo` паттерна (`Y+`, `Y_stack`, `X+`, …).

Для `k8s`: перед добавлением возможны `_enrich_k8s_minimal_row` / `_enrich_k8s_middle_row` в зависимости от `Diagram details`.

---

## Проход I — постобработка страницы

После всех паттернов на странице:

| Шаг | Действие | Где |
|-----|----------|-----|
| I1 | Порядок рёбер INET / EXT-WAN относительно DMZ | `reorder_inet_ext_wan_edge_before_dmz_swimlane` |
| I2 | Пересегментные файрволы поверх остальных | `bring_cross_segment_firewalls_to_front` |
| I3 | Подгонка подписи локации над зонами (первый раз) | `refresh_location_label_mxcells_after_swimlane_geometry` → `resize_location_label_to_cover_segments` |
| I4 | Раскладка КБ (слой 101), только `dc`/`office` | `auto_layout.kb_layout.kb_layout` |
| I5 | Раскладка сервисов ТА (слой 102), только `dc`/`office` | `auto_layout.services_ta_layout.services_TA_layout` |
| I6 | Расширение сегментов при переполнении LAN и сдвиг соседей | `expand_segments_for_lan_overflow_and_shift_neighbors` |
| I7 | Подпись локации после изменения геометрии сегментов | снова `refresh_location_label_mxcells_after_swimlane_geometry` |
| I8 | Рамки групп LAN (`lan_kant`) | `place_lan_group_kant_cells` |
| I9 | Подпись локации после возможного роста bbox | третий вызов `refresh_location_label_mxcells_after_swimlane_geometry` |

**Дополнительно по плану** (см. [plan-lan-overlay-segment-work.md](plan-lan-overlay-segment-work.md)): возможное подключение `user_devices_layout` и шагов из [lan-overlay-segment-algorithm.md](lan-overlay-segment-algorithm.md).

---

## Проход J — финиш запуска

| Шаг | Действие |
|-----|----------|
| J1 | `draw_verify` |
| J2 | `dump_file` → `output_file` |
| J3 | `advanced_analysis` |

---

## Сводка по одной странице (не Main Schema)

```text
C: корни страницы
  → D: edge_segments_layout (intrinsic → positions, segment_size)
  → E: dmz_segments_layout (+ segment_origin, cross_segment FW)
  → F: align_int_net_security_bottom_to_int_wan_edge
G: паттерны → get_object → add_pages → add_object / update_node → add_links
I: порядок рёбер → firewalls вперёд → метка локации → kb_layout → services_TA_layout → LAN overflow → метка → lan_kant → метка [dc/office]
```

Для **Main Schema** проходы D–F дают пустой кэш; опора на `compute_main_schema_segment_dimensions` и `position_offset` там, где intrinsic не задаёт координаты.

---

## Основные файлы

| Файл | Роль |
|------|------|
| `seaf2drawio.py` | Точка входа, цикл страниц, слияние кэша, постобработка |
| `auto_layout/edge_segments_layout.py` | Первый intrinsic-слой по зонам |
| `auto_layout/dmz_segments_layout.py` | Сетка зон, `segment_origin` |
| `auto_layout/segment_intrinsic_layout.py` | Ядро intrinsic, поля сегмента, выравнивание INT-NET |
| `auto_layout/kb_layout.py` | Пост-раскладка КБ |
| `auto_layout/services_ta_layout.py` | Пост-раскладка сервисов ТА |
| `auto_layout/layout_pattern_modes.py` | Режим interior для office/dc |

*Дата актуализации: 2026-05-06.*
