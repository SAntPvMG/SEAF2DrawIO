# Алгоритм приложения SEAF2DrawIO: уровни и переменные

Единый обзор **`seaf2drawio.py`**: фазы выполнения, слои раскладки, структура страницы Draw.IO и конфигурация. Детали формул intrinsic/DMZ см. в связанных документах в конце.

**Дата актуализации:** 2026-05-06.

---

## 1. Назначение

Приложение читает объединённые YAML-данные SEAF TA, базовый шаблон `.drawio` и паттерны из `data/patterns/*.yaml`, затем формирует многостраничную диаграмму (узлы, группы, связи) и записывает выходной файл.

Обратное преобразование выполняет отдельный скрипт **`drawio2seaf.py`** (см. секцию конфигурации `drawio2seaf` в `config.yaml`).

---

## 2. Типы «уровней» (что имеется в виду)

| Уровень | Смысл |
|--------|--------|
| **A–J** | Последовательность **проходов** выполнения программы (от старта до записи файла). |
| **Кэш автораскладки** | Три словаря `positions` / `segment_size` / `segment_origin` (+ вспомогательные поля) на одну итерацию страницы. |
| **Группы mxGraph** | Иерархия **`mxCell`** на странице: родитель `0` → слои `001`…`104` → объекты сегментов и оборудования. |
| **Слои UI Draw.IO** | Панель слоёв в редакторе соответствует **`value`** групп (`INTERNET`, `Connections`, …), не путать с проходами A–J. |

---

## 3. Проходы выполнения (A–J)

| Проход | Содержание |
|--------|------------|
| **A** | Старт: Python ≥ 3.9, загрузка конфига (`config.yaml` + `cli_vars`), сброс кэшей при необходимости, `diagram.from_xml` по `drawio_pattern`, `remove_obsolete_links`, заполнение `diagram_ids['Main Schema']` из схемы `root_object`. |
| **B** | Внешний цикл: `for file_name, pages in diagram_pages.items()` — файлы паттернов `main`, `office`, `dc`, `k8s`. Для **k8s** перед страницами: `_init_k8s_runtime_context()`, фильтрация паттернов по уровню детализации. |
| **C** | Подготовка страницы: `diagram.go_to_diagram(page_name)`, путь к YAML `data/patterns/{file_name}.yaml`, корни локации `_roots` из `diagram_ids[page_name]` или `resolve_page_location_roots`. |
| **D** | Первый слой раскладки (WAN / intrinsic): `compute_wan_edge_layout` → для не Main Schema вызывается intrinsic по набору зон; результат — начальное наполнение `wan_edge_layout_cache['positions']`, `['segment_size']`. |
| **E** | Второй слой (DMZ / сетка office и dc): `compute_dmz_layout` → слияние в тот же кэш, появление `segment_origin`, межсегментные файрволы (`cross_segment_firewall_oids`, `apply_cross_segment_firewall_positions`). |
| **F** | Выравнивание нижней границы INT-NET / INT-SECURITY относительно INT-WAN-EDGE: `align_int_net_security_bottom_to_int_wan_edge`. |
| **G** | Цикл по записям в YAML паттернов страницы: `get_object` → `add_pages` (если есть `ext_page`) → для каждого OID `update_node` / `add_object` → `add_links` для паттернов `network_links*` / `logical_links*`. |
| **H** | Внутри `add_object`: шаблон XML, `parent_id`, для Main Schema — колонки ISP и `compute_main_schema_segment_dimensions`; иначе координаты из кэша (`positions`, `segment_size`, `segment_origin`) или `position_offset` по полю `algo`. |
| **I** | **Постобработка страницы** (после всех паттернов): `reorder_inet_ext_wan_edge_before_dmz_swimlane` → `bring_cross_segment_firewalls_to_front` → **`refresh_location_label_mxcells_after_swimlane_geometry`** → для `dc`/`office`: **`kb_layout`** → **`services_TA_layout`** → **`expand_segments_for_lan_overflow_and_shift_neighbors`** → снова **`refresh_location_label_mxcells_after_swimlane_geometry`** → **`place_lan_group_kant_cells`** → ещё раз **`refresh_location_label_mxcells_after_swimlane_geometry`**. |
| **J** | Финиш: `draw_verify`, запись `output_file`, `advanced_analysis`. |

Для **Main Schema** проходы **D–F** дают пустой кэш; координаты части объектов задаются размерами сегментов и **`position_offset`**.

---

## 4. Кэш автораскладки (`wan_edge_layout_cache`)

На каждую страницу словарь переинициализируется в коде; ключи:

| Ключ | Назначение |
|------|------------|
| `positions` | OID → координаты и размеры внутренних объектов (LAN, компоненты внутри полос/зон), когда intrinsic задаёт размещение. |
| `segment_size` | OID **`network_segments`** → ширина/высота прямоугольника зоны (WAN-edge, DMZ, INT-NET, …), если включён авторазмер из раскладки. |
| `segment_origin` | OID сегмента → абсолютные **x, y** контейнера зоны на сетке (типично office после DMZ-прохода). |
| `cross_segment_firewall_oids` | Множество OID межсегментных файрволов для порядка отрисовки (см. `bring_cross_segment_firewalls_to_front`). |

Источники расчёта: **`auto_layout/edge_segments_layout.py`**, **`auto_layout/dmz_segments_layout.py`**, **`auto_layout/segment_intrinsic_layout.py`** (см. также `layout_pattern_modes.py`, кэш отпечатков в `layout_cache.py`).

---

## 5. Группы страницы Draw.IO (структурные id)

Каркас детальных страниц ЦОД/офиса задаётся шаблоном **`ext_page`** в `data/patterns/main.yaml` (и наследуется страницами `dc`/`office`). Типовые **родительские** группы привязаны к `parent="0"`:

| id (`mxCell`) | Назначение (поле `value`) |
|---------------|---------------------------|
| `001` | Основной swimlane локации (**DC** / аналог для офиса — «Office»): сегменты внутренней схемы, ярлык локации (`dc_label` / `office_label`) как дочерний `vertex` с `parent="001"`. |
| `002` | External Net |
| `003` | INTERNET (колонка внешних сетей на детальной странице) |
| `004` | WAN |
| `98`, `99` | Экосистема / Сбербанк (часто `visible="0"`) |
| `100` | Connections |
| `101` | Сервисы КБ |
| `102` | Тех. сервисы |
| `103` | Прикладные компоненты |
| `104` | Links |

На **Main Schema** в `data/base.drawio` используются свои группы (например `INTERNET`, `TRANSPORT-WAN` внутри колонок).

**Константы в коде** (`seaf2drawio.py`): `_SEGMENT_PARENT_CELL_ID = '001'` — родитель сегментов и ярлыка локации внутри основной группы страницы.

---

## 6. Конфигурация и переменные

### 6.1. Секция `seaf2drawio` в `config.yaml`

| Переменная | Назначение |
|------------|------------|
| `data_yaml_file` | Строка-путь или список путей: файл `.yaml/.yml` или каталог с такими файлами (слияние данных). |
| `drawio_pattern` | Базовый шаблон Draw.IO (обычно `data/base.drawio`). |
| `output_file` | Путь к результирующему `.drawio`. |
| `verify_generation` | Участие в проверках при генерации (через механизмы верификации в коде/линках). |
| `auto_layout_grid`, `auto_layout_script`, `auto_layout_diagram`, `auto_layout_filter` | Доп. параметры раскладки (при использовании внешнего сценария — см. код и комментарии в конфиге). |

### 6.2. Аргументы командной строки (`cli_vars`)

| Аргумент | Поле в конфиге |
|-----------|----------------|
| `-s` / `--src` | `data_yaml_file` |
| `-d` / `--dst` | `output_file` |
| `-p` / `--pattern` | `drawio_pattern` |

### 6.3. Значения по умолчанию в коде (`DEFAULT_CONFIG` в `seaf2drawio.py`)

Подставляются, если в пользовательском `config.yaml` ключ не переопределён:

- `data_yaml_file`: `data/example/test_seaf_ta_P41_v0.9.yaml`
- `drawio_pattern`: `data/base.drawio`
- `output_file`: `result/Sample_graph.drawio`
- `verify_generation`: `False`

### 6.4. Секция `drawio2seaf` в `config.yaml`

Используется скриптом **`drawio2seaf.py`**, не `seaf2drawio.py`:

| Переменная | Назначение |
|------------|------------|
| `schema_file` | Схема SEAF для разбора (`data/seaf_schema.yaml`). |
| `drawio_file` | Входной `.drawio`. |
| `output_file` | Выходной YAML. |

---

## 7. Глобальное состояние модуля `seaf2drawio.py`

| Имя | Назначение |
|-----|------------|
| `diagram` | Экземпляр `N2G.drawio_diagram`; переключение страниц через `go_to_diagram`. |
| `diagram_pages` | `{'main': [...], 'office': [...], 'dc': [...], 'k8s': [...]}` — очередь страниц по файлу паттернов; изначально заполнен только `main: ['Main Schema']`, остальное добавляет `add_pages`. |
| `diagram_ids` | Имя страницы → список OID, считающихся присутствующими на диаграмме (связи, фильтры `parent_id`). |
| `conf` | Итоговый словарь конфигурации после загрузки и CLI. |
| `pending_missing_links` | Пары связей, где цель не найдена на странице. |
| `wan_edge_layout_cache` | См. раздел 4. |
| `expected_counts`, `expected_data`, `pattern_specs` | Накопление для `advanced_analysis`. |
| `patterns_dir` | `'data/patterns/'`. |
| `root_object` | Схема для первичного заполнения Main Schema: `'seaf.company.ta.services.dc_regions'`. |
| `_k8s_*` | Режим детализации K8s, объединённая страница и вспомогательные индексы по данным. |

---

## 8. Переменные паттерна (`data/patterns/*.yaml`)

У каждой записи паттерна (например `network_segment_office`, `isp`, `dc_label`) типичные поля:

| Поле | Роль |
|------|------|
| `xml` | Шаблон XML узла/объекта с плейсхолдерами `{{…}}`. |
| `schema` | Ключ схемы в объединённых YAML для выборки объектов. |
| `type`, `parent_id`, `sort` | Фильтрация и порядок. |
| `require_fields` / `exclude_fields` | Доп. фильтры выборки. |
| `x`, `y`, `w`, `h`, `offset`, `deep`, `algo` | Базовые метрики и алгоритм стека при отсутствии координат из кэша (`algo`: `Y+`, `Y-`, `X+`, …). |
| `ext_page` | Шаблон новой диаграммы (страницы) при создании детализации. |

---

## 9. Ярлык локации (dc / office): константы привязки

В `seaf2drawio.py` для **`resize_location_label_to_cover_segments`** используются в том числе:

| Имя | Смысл |
|-----|--------|
| `_LABEL_STENCIL_MARK` | Фрагмент стиля стикера ярлыка: `vsdxID=13090`. |
| `_LABEL_ANCHOR_INET_EXT_ZONES` | Зоны для левого края подписи: `INET-EDGE`, `EXT-WAN-EDGE`. |
| `_LABEL_ANCHOR_INT_NET_ZONE` | Зона для верхней привязки: `INT-NET`. |
| `_LABEL_TO_SEGMENT_PAD`, `_LABEL_GAP_ABOVE_SEGMENTS`, … | Отступы и оценка текстового блока подписи. |

Подпись на `<object>` и геометрия вершины должны использовать **одинаковый `id`** у объекта и дочернего `mxCell` (см. `docs/discussion-dc-office-label-mxcell-sync.md`).

---

## 10. Сводная блок-схема потока данных

```text
config.yaml + CLI → conf
base.drawio → diagram
YAML данные + data/patterns/{main|office|dc|k8s}.yaml

Для каждой страницы:
  C → D → E → F  →  кэш positions / segment_size / segment_origin
  G → add_pages / add_object / add_links
  I → порядок рёбер, файрволы, ярлык локации, КБ, ТА, LAN overflow, lan_kant, повторный ярлык

J → draw_verify → файл .drawio → advanced_analysis
```

---

## 11. Связанные документы

| Файл | Содержание |
|------|------------|
| [seaf2drawio-algorithm.md](seaf2drawio-algorithm.md) | Пошаговое описание основного потока и функций |
| [seaf2drawio-implementation-passes.md](seaf2drawio-implementation-passes.md) | Проходы A–J с привязкой к файлам (обновляйте I при изменении пост-прохода) |
| [lan-overlay-segment-algorithm.md](lan-overlay-segment-algorithm.md) | Алгоритмы LAN / overflow / выравнивание сегментов |
| [discussion-dc-office-label-mxcell-sync.md](discussion-dc-office-label-mxcell-sync.md) | Согласование object label и mxCell |
| [AGENTS.md](../AGENTS.md) | Команды запуска и структура репозитория |

---

*Документ обобщает код и конфигурацию; при существенных изменениях `seaf2drawio.py` обновите разделы 3 и 9.*
