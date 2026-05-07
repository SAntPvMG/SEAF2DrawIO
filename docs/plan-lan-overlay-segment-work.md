# План работ: пост-проход LAN / оверлеи / сегменты

Цель: привести репозиторий к алгоритму из [lan-overlay-segment-algorithm.md](lan-overlay-segment-algorithm.md).

## Текущее состояние репозитория (на момент составления плана)

- В `seaf2drawio.py` для `dc`/`office` после `resize_location_label_to_cover_segments` вызываются **`kb_layout`** и **`services_TA_layout`**.
- Модули **`user_devices_layout`**, **`lan_vertical_fit_overlays`**, **`lan_spread_overlapping_lans`**, **`lan_segment_align_expand_fit`** в каталоге `auto_layout/` отсутствуют — их нужно добавить или восстановить из ветки.

## Задачи

### 1. Восстановить недостающие модули

- [ ] `auto_layout/user_devices_layout.py` — раскладка `seaf.company.ta.components.user_devices` по `network_connection`.
- [ ] `auto_layout/lan_vertical_fit_overlays.py` — удлинение LAN под суммарную высоту оверлеев.
- [ ] `auto_layout/lan_overlap_spread.py` — разводка пересекающихся LAN и сдвиг оверлеев по якорю.
- [ ] `auto_layout/lan_segment_center_fit.py` (или эквивалентное имя) — расширение сегмента по полному union, центрирование блока LAN+оверлеев, contain.

При необходимости вынести общие функции (визуальный bbox LAN с учётом `rotation`, первичная вершина объекта LAN) в один модуль, чтобы избежать циклических импортов.

### 2. Подключить пайплайн в `seaf2drawio.py`

Строгий порядок вызовов после `resize_location_label_to_cover_segments`:

1. `kb_layout`
2. `services_TA_layout`
3. `user_devices_layout`
4. `lan_vertical_fit_network_connection_overlays`
5. `lan_spread_overlapping_lans`
6. `lan_segment_align_expand_fit`

### 3. Экспорт (опционально)

- [ ] Обновить `auto_layout/__init__.py`, если нужны публичные имена для AGENTS / внешних скриптов.

### 4. Документация

- [ ] В `docs/seaf2drawio-algorithm.md` добавить краткий подраздел со ссылкой на [lan-overlay-segment-algorithm.md](lan-overlay-segment-algorithm.md) и перечислением шагов пост-прохода.

### 5. Проверка

- [ ] `python -X utf8 seaf2drawio.py` без неожиданных ошибок.
- [ ] Визуально: страницы DC и офис — LAN и оверлеи не вылезают из inner сегмента, нет некорректных наложений полосок LAN после разводки.

### 6. Intrinsic: вертикальные колонки LAN (`deep`, `offset`)

По [lan-vertical-columns-layout-spec.md](lan-vertical-columns-layout-spec.md): колонки по **`deep + 3`**, зазор **`offset × 2`**.

- [x] Реализовать колоночную вертикальную раскладку в `auto_layout/segment_intrinsic_layout.py`.
- [x] Обновить комментарий в шапке `segment_intrinsic_layout.py` и bump ключа кэша intrinsic.

## Критерии готовности

- Все шесть функций пост-прохода вызываются в указанном порядке для `dc` и `office`.
- Описание алгоритма и план синхронизированы с кодом (обновить этот файл при изменении контрактов).

Дата плана: 2026-05-04.
