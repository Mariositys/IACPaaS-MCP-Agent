# Соответствие MCP-тулзов и IACPaaS

Источник по API: `https://iacpaas.dvo.ru/docs/RemoteCalls/RemoteCalls.html` и
`https://iacpaas.dvo.ru/v3/api-docs`.

| MCP-тулз | IACPaaS | Реализация |
|---|---|---|
| `list_inforesources` | — локальный `catalog.json` | поиск по `title`/`path`; не живой список фонда |
| `get_inforesource` | `GET /api/data/export/user-item` | сервер; `depth=0` для metadata |
| `get_subgraph` | `GET /api/data/export/user-item` + локальный обход | сервер |
| `count_graph` | `GET /api/data/export/user-item` + локальный подсчет | сервер, **аналога в API нет** |
| `query_graph` | `GET /api/data/export/user-item` + локальный поиск | сервер, **аналога в API нет** |
| `validate_answer` | — локальная проверка | сервер; сверяет `AgentAnswer` с evidence процесса по `call_id` |
| `import_inforesource` | `POST /api/data/import` | сервер, требуется `confirm=true` |
| `update_vertex` | `PUT /api/data/update` | сервер, требуется `confirm=true` |
| `delete_inforesource` | `DELETE /api/data/delete` | сервер, требуется `confirm=true` |

## Ограничения API

- Эндпоинта «список ИР / поиск по фонду» нет. `list_inforesources` ищет только
  по локальному `catalog.json`; отсутствие в каталоге не значит отсутствие на платформе.
- `count_graph`, `query_graph`, `get_subgraph` не являются методами платформы:
  они считаются по экспортированному дереву.
- `export-depth` и `start-target-concept-path` передаются строками, как в OpenAPI.
- `compress=true` возвращает zip, поэтому не используется.
- Таймаут 60 секунд; документация требует не меньше 30.
- Сервисный API (`run`, `stop`, `running`, `import-run-export`) вне задачи.
- `mark` у `update_vertex` обязателен.

## Авторизация

- `X-API-KEY` из `IACPAAS_API_KEY`, либо
- логин/пароль из `IACPAAS_USERNAME` / `IACPAAS_PASSWORD` через `POST /api/signin`
  с JWT и однократным повторным входом при 401.
