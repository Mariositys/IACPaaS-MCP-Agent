# Описание MCP-тулзов

MCP-сервер предоставляет 14 тулзов: 7 для чтения графа, `validate_answer`,
3 preview-тулза и 3 внутренних apply-тулза.

В колонке «Аргументы» `?` означает необязательный аргумент; в скобках указано
значение по умолчанию. `path` — путь к ИР внутри «Мой Фонд», например
`Загрузки/База металлопорошковых материалов`.

| Тулз | Что делает | IACPaaS / источник | Аргументы |
|---|---|---|---|
| `list_inforesources` | Ищет ИР по локальному каталогу | `catalog.txt` | `query?` — подстрока `title`/`path` (`""`), `limit?` (20, до 50) |
| `get_inforesource` | Возвращает метаданные или полный JSON ИР | `GET /api/data/export/user-item` | `path`, `format?` (`metadata`/`json`, по умолчанию `metadata`), `json_type?` (`universal`), `refresh?` (`false`) |
| `get_subgraph` | Возвращает подграф от вершины | Локальный кэш экспорта | `path`, `start?` (`""` — корень), `depth?` (2, до 10), `limit?` (100), `offset?` (0), `view?` (`compact`/`summary`/`full`, по умолчанию `summary`), `refresh?` (`false`) |
| `count_graph` | Считает вершины и дуги | Локальный кэш экспорта | `path`, `start?` (`""` — весь ИР), `refresh?` (`false`) |
| `search_vertices` | Ищет вершины по названию или мете | Локальный кэш экспорта | `path`, `text`, `by?` (`label`/`meta`, по умолчанию `label`), `exact?` (`false`), `start?` (`""`), `limit?` (50), `offset?` (0), `view?` (`compact`/`summary`/`full`, по умолчанию `compact`), `refresh?` (`false`) |
| `locate_vertex` | Находит вершину, её `path` и потомков | Локальный кэш экспорта | `path`, `text`, `limit?` (5, до 50), `start?` (`""`), `depth?` (2, до 10), `view?` (`compact`/`summary`/`full`, по умолчанию `compact`) |
| `get_terminal_values` | Возвращает плоский список «свойство → значение» | Локальный кэш экспорта | `path`, `start?` (`""`), `starts?` — список веток, `depth?` (4, до 10), `limit?` (100), `offset?` (0), `max_chars?` (12000), `include_path?` (`false`), `refresh?` (`false`) |
| `validate_answer` | Проверяет `AgentAnswer` по evidence текущей сессии | Локальная проверка | `answer` — объект `AgentAnswer` |
| `preview_import` | Готовит план импорта ИР | `POST /api/data/import` | `folder` — папка без имени ИР, `resource` — `InfoResourceDocument`, `clear_if_exists?` (`false`) |
| `preview_update` | Готовит план изменения метки/комментария вершины | `PUT /api/data/update` | `path`, `concept_path`, `metaconcept_path`, `mark`, `comment?` |
| `preview_delete` | Готовит план удаления или очистки | `DELETE /api/data/delete` | `path`, `mode` (`deleteIR`/`trashIR`/`clearIR`/`deleteSubGraph`/`clearSubGraph`), `concept_path?`, `metaconcept_path?` — обязательны для `*SubGraph` |
| `apply_import` | Импортирует ИР | `POST /api/data/import` | `folder`, `resource`, `confirmation_token`, `clear_if_exists?` (`false`) |
| `apply_update` | Изменяет метку/комментарий вершины | `PUT /api/data/update` | `path`, `concept_path`, `metaconcept_path`, `mark`, `confirmation_token`, `comment?` |
| `apply_delete` | Удаляет или очищает ИР/подграф | `DELETE /api/data/delete` | `path`, `mode`, `confirmation_token`, `concept_path?`, `metaconcept_path?` |

`preview_*` не меняют данные. `apply_*` не входят в набор тулзов модели: их вызывает
веб-сервер после кнопки «Подтвердить» или CLI после ответа `y`.

## Ограничения API

- Эндпоинта «список ИР / поиск по фонду» нет: `list_inforesources` ищет только по
  локальному `catalog.txt`.
- `get_subgraph`, `count_graph`, `search_vertices`, `locate_vertex` и
  `get_terminal_values` — серверные операции над полным экспортом ИР, а не методы
  платформы.
- На текущем стенде `PUT /api/data/update` отклоняет `action: updateVertice`,
  поэтому `apply_update` завершается ошибкой `kind="platform"`.
