"""Адаптер к API IACPaaS: по методу на операцию платформы, ошибки - в ToolError."""

import json
from typing import Any

import httpx2
from fastmcp.exceptions import ToolError

from .config import API_KEY, BASE_URL, PASSWORD, TIMEOUT, USERNAME


class IacpaasClient:
    def __init__(self):
        self._token: str | None = None
        self._http = httpx2.AsyncClient(
            base_url=BASE_URL,
            headers={"Accept": "application/json"},
            timeout=TIMEOUT,
        )

    # ---------- операции платформы ----------

    async def export(
        self,
        path: str,
        json_type: str = "universal",
        start_concept_path: str | None = None,
        depth: int | None = None,
        no_blob_data: bool = True,
    ) -> dict[str, Any]:
        params = {
            "path": path,
            "json-type": json_type,
            "start-target-concept-path": start_concept_path,
            "export-depth": depth,
            "no-blob-data": no_blob_data,
        }
        data = await self._call(
            "GET", "/api/data/export/user-item", params={k: v for k, v in params.items() if v is not None}
        )
        # В OpenAPI ответ - просто object: это либо сам ИР, либо обёртка ExportResult с JSON строкой
        if "result" in data and "success" in data:
            return json.loads(data["result"])
        return data

    async def import_(self, folder: str, document: dict[str, Any], clear_if_exists: bool) -> dict[str, Any]:
        body = {
            "path": folder,
            "json": json.dumps(document, ensure_ascii=False),
            "clearIfExists": clear_if_exists,
        }
        return await self._call("POST", "/api/data/import", json=body)

    async def update(
        self, path: str, concept_path: str, metaconcept_path: str, mark: str, comment: str | None
    ) -> dict[str, Any]:
        body = {
            "path": path,
            "concept_path": concept_path,
            "metaconcept_path": metaconcept_path,
            "mark": mark,
        }
        if comment is not None:
            body["comment"] = comment
        return await self._call("PUT", "/api/data/update", json=body)

    async def delete(
        self, path: str, mode: str, concept_path: str | None, metaconcept_path: str | None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"path": path, "mode": mode}
        if concept_path is not None and metaconcept_path is not None:
            body["concept_path"] = concept_path
            body["metaconcept_path"] = metaconcept_path
        return await self._call("DELETE", "/api/data/delete", json=body)

    async def aclose(self) -> None:
        await self._http.aclose()

    # ---------- HTTP и авторизация ----------

    async def _call(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        try:
            resp = await self._request(method, url, **kwargs)
        except httpx2.TimeoutException as e:
            raise ToolError(f"IACPaaS не ответил за {TIMEOUT} с ({url})") from e
        except httpx2.HTTPError as e:
            raise ToolError(f"IACPaaS недоступен ({url}): {e}") from e

        if resp.status_code in (401, 403):
            raise ToolError(f"IACPaaS отказал в доступе ({resp.status_code}, {url}): проверьте ключ или логин")
        if resp.is_error:
            raise ToolError(f"IACPaaS вернул {resp.status_code} ({url}): {resp.text[:500]}")
        try:
            data = resp.json()
        except ValueError as e:
            raise ToolError(f"IACPaaS вернул не JSON ({url}): {resp.text[:500]}") from e

        # Result/CodeResult: ошибка приходит с кодом 200 и success=false
        if isinstance(data, dict) and data.get("success") is False:
            if data.get("platformUnavailable"):
                raise ToolError(f"Платформа IACPaaS временно недоступна ({url})")
            details = data.get("explanation") or data.get("explanationSafe")
            raise ToolError(f"IACPaaS ({url}): {data.get('error') or 'ошибка'}" + (f". {details}" if details else ""))
        return data

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx2.Response:
        resp = await self._http.request(method, url, headers=await self._auth(), **kwargs)
        if resp.status_code == 401 and self._token:
            # JWT мог истечь: один раз логинимся заново
            self._token = None
            resp = await self._http.request(method, url, headers=await self._auth(), **kwargs)
        return resp

    async def _auth(self) -> dict[str, str]:
        if API_KEY:
            return {"X-API-KEY": API_KEY}
        if self._token is None:
            self._token = await self._signin()
        return {"Authorization": f"Bearer {self._token}"}

    async def _signin(self) -> str:
        if not (USERNAME and PASSWORD):
            raise ToolError("Не задана авторизация: IACPAAS_API_KEY или IACPAAS_USERNAME/IACPAAS_PASSWORD")
        resp = await self._http.post("/api/signin", json={"username": USERNAME, "password": PASSWORD})
        token = resp.json().get("accessToken") if resp.status_code == 200 else None
        if not token:
            raise ToolError(f"Вход в IACPaaS не удался ({resp.status_code}): {resp.text[:300]}")
        return token
