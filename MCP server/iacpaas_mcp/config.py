"""Настройки сервера. Читаются из переменных окружения с префиксом IACPAAS_ или из .env."""

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="IACPAAS_", env_file=".env", extra="ignore")

    # API IACPaaS (RemoteCalls.html, /v3/api-docs)
    base_url: str = "https://iacpaas.dvo.ru"
    # Авторизация: ключ от администратора (X-API-KEY) или логин/пароль для /api/signin (JWT)
    api_key: str | None = None
    username: str | None = None
    password: str | None = None
    # Платформа отвечает до 30 с, таймаут меньше 30 с документация запрещает
    timeout: float = 60.0

    # Политика записи: write-тулзы отказывают, пока это не включено
    allow_write: bool = False

    # Верхние границы для запросов к графу
    max_limit: int = 500
    max_depth: int = 10

    # Транспорт MCP: stdio для IDE, http (Streamable HTTP) для удалённого запуска
    transport: Literal["stdio", "http"] = "stdio"
    host: str = "127.0.0.1"
    port: int = 8000


settings = Settings()
