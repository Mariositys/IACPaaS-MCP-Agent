"""Контракт ответа агента: каждое утверждение обязано ссылаться на результат тулза."""

from pydantic import BaseModel, Field, field_validator


class Source(BaseModel):
    call_id: str = Field(min_length=1, description="id вызова тулза в этом запуске")
    pointer: str = Field(description="JSON Pointer до значения в результате; '' - весь результат")
    expr: str | None = Field(None, description="Формула для производного значения, например 'ref(...) / 2'")

    @field_validator("pointer")
    @classmethod
    def check_pointer(cls, value: str) -> str:
        if value and not value.startswith("/"):
            raise ValueError("JSON Pointer должен быть пустым или начинаться с '/'")
        return value


class Claim(BaseModel):
    statement: str = Field(min_length=1, description="Утверждение на естественном языке")
    value: bool | int | float | str | None = Field(None, description="Значение, если утверждение про число или строку")
    sources: list[Source] = Field(min_length=1, description="Хотя бы один источник")


class AgentAnswer(BaseModel):
    answer: str = Field(min_length=1)
    claims: list[Claim] = Field(default_factory=list)
    refused: bool = Field(False, description="True, если данных для ответа не хватило")
