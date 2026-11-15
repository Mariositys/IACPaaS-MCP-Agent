"""Контракт ответа агента: claims содержат только прямые данные из тулзов."""

from pydantic import BaseModel, Field, field_validator


class Source(BaseModel):
    """Ссылка на прямой узел результата тулза; вычисленные значения не допускаются."""

    call_id: str = Field(min_length=1, description="id вызова тулза в этом запуске")
    pointer: str = Field(description="JSON Pointer до значения в результате; '' - весь результат")

    @field_validator("pointer")
    @classmethod
    def check_pointer(cls, value: str) -> str:
        if value and not value.startswith("/"):
            raise ValueError("JSON Pointer должен быть пустым или начинаться с '/'")
        return value


class Claim(BaseModel):
    """Прямой факт из результата тулза; суммы, средние и другие вычисления не допускаются."""

    statement: str = Field(min_length=1, description="Смысл утверждения без самого значения; значение задается в value или quote")
    value: bool | int | float | str | None = Field(None, description="Точное значение из источника, без агрегатов и вычислений")
    quote: str | None = Field(None, min_length=1, description="Точная цитата из источника для текстового утверждения")
    sources: list[Source] = Field(min_length=1, description="Хотя бы один источник")


class AgentAnswer(BaseModel):
    answer: str = Field(min_length=1)
    claims: list[Claim] = Field(default_factory=list)
    refused: bool = Field(False, description="True, если данных для ответа не хватило")
    requires_confirmation: bool = Field(
        False,
        description="True, если write-тулз вернул OperationPreview и нужно подтверждение пользователя",
    )
