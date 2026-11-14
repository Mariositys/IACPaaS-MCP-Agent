# Соответствие MCP-тулзов и IACPaaS

Источник по API: `https://iacpaas.dvo.ru/docs/RemoteCalls/RemoteCalls.html` и
`https://iacpaas.dvo.ru/v3/api-docs`.

| MCP-тулз | IACPaaS | Реализация |
|---|---|---|
| `list_inforesources` | — локальный `catalog.txt` | поиск по `title`/`path`; не живой список фонда |
| `get_inforesource` | `GET /api/data/export/user-item` | первый раз скачивает полный ИР в локальный кэш; `metadata` отдает корень |
| `get_subgraph` | локальный кэш полного экспорта | сервер; страницы `offset`/`next_offset`, без повторного скачивания |
| `count_graph` | локальный кэш полного экспорта | сервер, **аналога в API нет** |
| `search_vertices` | локальный кэш полного экспорта | сервер; `by="label|meta"`, компактные узлы по умолчанию, страницы `offset`/`next_offset`, **аналога в API нет** |
| `locate_vertex` | локальный кэш полного экспорта | сервер; поиск + явный `path` + потомки до `depth` одним вызовом |
| `get_terminal_values` | локальный кэш полного экспорта | сервер; плоский список «свойство → значение» для поддерева, **аналога в API нет** |
| `validate_answer` | — локальная проверка | сервер; сверяет `AgentAnswer` с evidence процесса по `call_id` |
| `import_inforesource` | `POST /api/data/import` | сервер, требуется `confirm=true` |
| `update_vertex` | `PUT /api/data/update` | сервер, требуется `confirm=true` |
| `delete_inforesource` | `DELETE /api/data/delete` | сервер, требуется `confirm=true` |

## Ограничения API

- Эндпоинта «список ИР / поиск по фонду» нет. `list_inforesources` ищет только
  по локальному `catalog.txt`; отсутствие в каталоге не значит отсутствие на платформе.
- `count_graph`, `search_vertices`, `get_subgraph`, `locate_vertex`, `get_terminal_values`
  не являются методами платформы:
  они работают по полному экспорту, сохраненному в `data/graph-cache`.
- Кэш живет 300 секунд; `refresh=true` перечитывает ресурс.
- `export-depth` и `start-target-concept-path` передаются строками, как в OpenAPI.
- `compress=true` возвращает zip, поэтому не используется.
- Таймаут 60 секунд; документация требует не меньше 30.
- Сервисный API (`run`, `stop`, `running`, `import-run-export`) вне задачи.
- `mark` у `update_vertex` обязателен; тулз меняет только `mark`/`comment`.
- На текущем стенде `PUT /api/data/update` возвращает `action: updateVertice`,
  поэтому `update_vertex` завершается ошибкой `kind="platform"`.

## Авторизация

- `X-API-KEY` из `IACPAAS_API_KEY`, либо
- логин/пароль из `IACPAAS_USERNAME` / `IACPAAS_PASSWORD` через `POST /api/signin`
  с JWT и однократным повторным входом при 401.
