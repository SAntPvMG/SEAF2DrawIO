# Алгоритм работы приложения `seaf2drawio.py`

Документ описывает полный поток выполнения генератора диаграмм Draw.io из данных SEAF (YAML) по состоянию кода в репозитории.

---

## 1. Назначение

Скрипт `seaf2drawio.py`:

1. Загружает объединённые YAML-данные архитектуры (SEAF TA).
2. Читает базовый шаблон Draw.io (`drawio_pattern`, по умолчанию `data/base.drawio`).
3. Для каждой страницы диаграммы применяет описания из `data/patterns/{main|office|dc}.yaml`.
4. Строит узлы (сети, сегменты, компоненты, сервисы и т.д.) и связи.
5. Записывает результат в выходной `.drawio` и при необходимости выводит отчёт верификации.

Для страниц ЦОД и офиса координаты и размеры части объектов задаются модулем **`auto_layout`** (intrinsic-раскладка полос WAN/LAN и сетка зон офиса).

---

## 2. Зависимости

| Компонент | Роль |
|-----------|------|
| **N2G** (`drawio_diagram`) | Построение XML диаграммы, узлы, связи, страницы |
| **lib.seaf_drawio.SeafDrawio** | Конфигурация, чтение YAML, выборка объектов по схеме/типу, разбор XML-шаблонов узлов |
| **lib.link_manager** | `remove_obsolete_links`, `draw_verify`, `advanced_analysis` |
| **auto_layout.edge_segments_layout** | Первый проход: `compute_wan_edge_layout` — позиции и размеры для WAN-edge (и для офиса — внутренних зон) |
| **auto_layout.dmz_segments_layout** | Второй проход: `compute_dmz_layout` — DMZ и для **office.yaml** абсолютные координаты сетки зон (`segment_origin`) |

---

## 3. Конфигурация и аргументы командной строки

- По умолчанию используется `DEFAULT_CONFIG` в коде и **`config.yaml`** (секция `seaf2drawio`).
- Функция **`cli_vars(config)`** (`seaf2drawio.py`) расширяет конфиг аргументами:
  - `-s` / `--src` — файл или каталог с YAML данными;
  - `-d` / `--dst` — путь к выходному `.drawio`;
  - `-p` / `--pattern` — базовый шаблон Draw.io.

---

## 4. Глобальное состояние (ключевые переменные модуля)

| Переменная | Назначение |
|------------|------------|
| `diagram` | Экземпляр `drawio_diagram`; текущая страница переключается `go_to_diagram` |
| `diagram_pages` | Имя файла паттернов (`main`, `office`, `dc`) → список имён страниц; изначально задано только `main: ['Main Schema']`, остальные страницы добавляет **`add_pages`** |
| `diagram_ids` | На каждую страницу — множество OID объектов, которые считаются присутствующими на диаграмме (для связей и фильтрации) |
| `conf` | Итоговая конфигурация после загрузки и CLI |
| `wan_edge_layout_cache` | На **каждую итерацию страницы** заново вычисляется: словари `positions`, `segment_size`, `segment_origin` |
| `pending_missing_links` | Кортежи `(страница, источник, цель)`, когда цель связи не найдена на странице |
| `expected_counts`, `expected_data`, `pattern_specs` | Накопление ожидаемых объектов для **`advanced_analysis`** |
| `patterns_dir` | `'data/patterns/'` |
| `root_object` | `'seaf.company.ta.services.dc_regions'` — схема для первичного заполнения `diagram_ids['Main Schema']` |

---

## 5. Точка входа: `if __name__ == '__main__'`

Последовательность:

1. Проверка **Python ≥ 3.9**.
2. Загрузка конфига: `conf = cli_vars(d.load_config("config.yaml")['seaf2drawio'])`.
3. Сброс кэша раскладки Main Schema: `_main_isp_layout_cache = None`.
4. **`diagram.from_xml(d.read_file_with_utf8(conf['drawio_pattern']))`** — загрузка базового XML.
5. **`remove_obsolete_links(diagram, conf['data_yaml_file'], ...)`** — удаление устаревших связей по правилам для компонентной схемы.
6. **`diagram_ids['Main Schema']`** = список ключей объектов `root_object` из данных.
7. Двойной цикл: **`for file_name, pages in diagram_pages.items():`** → **`for page_name in pages:`** — см. раздел 6.

После всех страниц:

8. **`draw_verify(diagram_ids, diagram, pending_missing_links)`**.
9. **`d.dump_file(...)`** — запись `conf['output_file']`.
10. **`advanced_analysis(conf, expected_counts, expected_data, pattern_specs, d)`**.

---

## 6. Цикл по страницам и паттернам

Для каждой пары `(file_name, pages)` и каждого `page_name`:

### 6.1. Подготовка страницы

- `diagram.go_to_diagram(page_name)`.
- Путь к YAML паттернов страницы: `data/patterns/{file_name}.yaml`.

### 6.2. Корни локации страницы (`page_roots`)

- `_roots = diagram_ids.get(page_name)` или, если пусто, **`resolve_page_location_roots(d, conf, page_name, _py)`** — OID локации страницы по её `title` в данных (как при создании страниц в `add_pages`).

### 6.3. Предрасчёт раскладки (до отрисовки объектов паттернами)

1. **`wan_edge_layout_cache = compute_wan_edge_layout(...)`** (`edge_segments_layout`):
   - для **Main Schema** возвращает пустые словари;
   - иначе вызывает **`compute_intrinsic_band_layout`** с набором зон по имени файла паттернов (`office.yaml` — расширенный набор с DMZ / INT-NET / INT-SECURITY-NET).

2. **`_dmz_layout = compute_dmz_layout(..., wan_edge_layout_cache)`**:
   - для **office** дополняет/использует кэш и задаёт **`segment_origin`** для контейнеров зон на сетке;
   - для **dc** при необходимости отдельный intrinsic для DMZ.

3. Результаты сливаются в **`wan_edge_layout_cache`** (`positions`, `segment_size`, `segment_origin`).

### 6.4. Цикл по записям в YAML паттернов

Файл читается как словарь `имя_паттерна → описание`.

Для каждого `k, object_pattern`:

1. **`object_data = d.get_object(...)`** — выборка из данных по `schema`, опционально `type`, сортировка по `parent_id`, фильтры `require_fields` / `exclude_fields`.

2. **`add_pages(object_pattern)`** — если задан `ext_page`, создаются новые диаграммы (страницы) и пополняются `diagram_pages` и `diagram_ids`.

3. Сброс счётчиков паттерна (`count`, `last_parent`, `parent`), очистка `_isp_y_*` для колонок ISP на Main Schema.

4. **`collect_ids()`** — учёт ожидаемых OID для диагностики.

5. Для каждого `i` в `object_data.keys()`:
   - если узел `i` уже есть в текущей диаграмме — **`diagram.update_node`** и добавление в `diagram_ids`;
   - иначе — **`add_object(object_pattern, object_data[i], i)`**.

6. Если имя паттерна совпадает с **`network_links(_число)?`** — **`add_links(object_pattern)`**.

7. Если имя совпадает с **`logical_links(_число)?`** — **`add_links(..., logical_link=True)`**.

---

## 7. Функция `add_pages(pattern)`

Условие: **`pattern.get('ext_page')`**.

- Читаются объекты схемы `pattern['schema']`.
- Для каждого `key_id` добавляется страница диаграммы с заголовком из `title`, XML страницы из шаблона `ext_page`.
- В **`diagram_pages[ключ_файла_из_внешнего_цикла]`** добавляется название страницы.
- В **`diagram_ids[title]`** добавляется OID локации.

Так формируются списки страниц для `office`, `dc` и т.д.

---

## 8. Функция `add_object(pattern, data, key_id)`

Размещение одной сущности на текущей странице.

1. Для каждого XML-фрагмента из **`d.get_xml_pattern(pattern['xml'], key_id)`** подставляется шаблон узла.

2. **Родитель (`parent_id`)**: если указан, проверяется пересечение значения поля в данных с **`diagram_ids[page_name]`**; выбирается текущий родитель (в т.ч. нормализация списковых полей).

3. На **Main Schema** при смене «зоны» родителя (`INTERNET` / `TRANSPORT-WAN`) сохраняются вертикальные позиции **`_isp_y_internet`** / **`_isp_y_transport`**, чтобы колонки WAN не перекрывались при переборе сегментов.

4. **`format_map`** заполняет плейсхолдеры в XML данными объекта, `Group_ID`, родителем и т.д.

5. Если **`key_id` уже в `diagram_ids[page_name]`** (объект разрешён для отрисовки на странице):

   - при необходимости `id` → `sid`;
   - для **Main Schema** и зон **INTERNET / TRANSPORT-WAN** вызывается **`compute_main_schema_segment_dimensions`** — размер контейнера под число WAN и раскладку `isp` из `main.yaml`;
   - из **`wan_edge_layout_cache`** последовательно:
     - **`positions[key_id]`** → `x, y, w, h` (LAN, устройства внутри сегмента);
     - **`segment_size[key_id]`** при **`_is_segment_auto_size_from_layout`** → размер прямоугольника зоны сегмента;
     - **`segment_origin[key_id]`** → абсолютные **`x, y`** контейнера зоны (офисная сетка);
   - **`diagram.add_node`** с координатами и размерами;
   - если первый фрагмент и для `key_id` нет ни позиции из кэша, ни `segment_origin`, вызывается **`position_offset(pattern)`** — смещение по алгоритму **`algo`** (`Y+`, `Y-`, `X+`, `X-`) для стекования экземпляров.

---

## 9. Функция `position_offset(pattern)`

Сдвигает координаты паттерна для следующего по счёту объекта того же типа по правилам **`algo`** и параметрам **`offset`**, **`deep`**, размеров **`w`/`h`**. Используется, когда координаты не заданы модулем автораскладки.

---

## 10. Функция `add_links(pattern, **kwargs)`

- Обходит записи схемы связей из данных.
- При **`logical_link=True`** источник может браться из полей записи.
- Если задан **`parent_id`**, список целей может строиться из поля **`targets`** (например `location`) или через цепочку **`get_parent_value`**.
- Связь добавляется только если источник и цель есть в **`diagram_ids[page_name]`**; иначе пара попадает в **`pending_missing_links`**.

---

## 11. Вспомогательные проверки типов сегментов

- **`_is_main_schema_zone_segment`** — паттерн сегмента с типом `zone:INTERNET` или `zone:TRANSPORT-WAN`.
- **`_is_segment_auto_size_from_layout`** — зоны, для которых ширина/высота контейнера подставляются из **`segment_size`**: WAN-edge, DMZ, INT-NET, INT-SECURITY-NET.

---

## 12. Связь с `auto_layout`

| Модуль | Роль |
|--------|------|
| **`segment_intrinsic_layout.compute_intrinsic_band_layout`** | По `network_segments`, `networks`, `components` считает позиции LAN и оборудования внутри прямоугольников зон из паттерна; при необходимости расширяет размеры сегментов; результат кэшируется по отпечатку данных и паттерна |
| **`edge_segments_layout`** | Выбирает набор зон по имени `office.yaml` / `dc.yaml` и вызывает intrinsic |
| **`dmz_segments_layout`** | Для офиса — сетка зон и **`segment_origin`**; слияние с результатами WAN |

Итоговые **`positions`** применяются к OID сетей и компонентов; **`segment_size`** и **`segment_origin`** — к OID **`network_segments`** для контейнеров зон.

---

## 13. Сводная схема потока данных

```text
config.yaml + CLI
       → conf
base.drawio → diagram
YAML данные + data/patterns/*.yaml

Для каждой страницы:
       → wan_edge_layout_cache (intrinsic + dmz/office grid)

Для каждого паттерна на странице:
       → объекты данных → add_pages / add_object / add_links

       → draw_verify → запись .drawio → advanced_analysis
```

---

## 14. Детализация проходов реализации

Пошаговое описание проходов A–J (старт, intrinsic, dmz, паттерны, `add_object`, постобработка страницы, финиш) и привязка к файлам: **[seaf2drawio-implementation-passes.md](seaf2drawio-implementation-passes.md)**.

---

## 15. Связанные файлы в репозитории

| Путь | Смысл |
|------|--------|
| `config.yaml` | Пути к данным, шаблону, выходному файлу |
| `data/patterns/main.yaml`, `office.yaml`, `dc.yaml` | Паттерны узлов и связей по страницам |
| `data/base.drawio` | Базовый XML Draw.io |
| `auto_layout/*.py` | Раскладка сегментов |
| `AGENTS.md` | Краткие правила проекта и команды запуска |
| `docs/lan-overlay-segment-algorithm.md` | Целевой пост-проход dc/office: КБ, ТА, User Devices, LAN, сегменты |
| `docs/plan-lan-overlay-segment-work.md` | План работ по внедрению этого пост-прохода |
| `docs/seaf2drawio-implementation-passes.md` | Полный алгоритм по проходам A–J и модулям |
| `docs/intrinsic-lan-horizontal-bands.md` | История: горизонтальная полоса LAN при count > deep (**устаревает** под вертикальные колонки) |
| `docs/lan-vertical-columns-layout-spec.md` | Спецификация: вертикальные колонки LAN (`deep+3`, `offset×2`) |

---

*Документ отражает логику `seaf2drawio.py`; номера строк привязаны к структуре файла и могут слегка изменяться при правках кода.*
