"""Данные запросов до HTTP-сериализации; frozen не замораживает вложенный JSON."""

from dataclasses import dataclass
from typing import Literal, TypeAlias

from rattle.models import HttpMethod, ParamLocation

JSONValue: TypeAlias = str | int | float | bool | None | list["JSONValue"] | dict[str, "JSONValue"]
JSONPath: TypeAlias = tuple[str | int, ...]
MutationKind = Literal[
    "missing_required",
    "wrong_type",
    "outside_enum",
    "below_minimum",
    "above_maximum",
    "too_short",
    "too_long",
]
MutationLocation = Literal["path", "query", "header", "cookie", "body"]
GenerationWarningKind = Literal["omitted_optional", "empty_array"]


@dataclass(frozen=True)
class RequestParameter:
    name: str
    location: ParamLocation
    value: JSONValue
    style: str
    explode: bool
    allow_reserved: bool = False


@dataclass(frozen=True)
class GenerationWarning:
    """Причина пропуска при построении базы; путь имеет формат MutationCase.path."""

    kind: GenerationWarningKind
    location: MutationLocation
    path: JSONPath
    message: str


@dataclass(frozen=True)
class RequestData:
    """Данные до HTTP: path — шаблон, has_body отличает отсутствие тела от JSON null.

    Удаление path-параметра — логическая мутация, не инструкция удалить сегмент URL.
    Политика отправки такого варианта пока не определена.
    warnings описывают построение базы, а не намеренные мутации запроса.
    """

    method: HttpMethod
    path: str
    parameters: tuple[RequestParameter, ...] = ()
    body: JSONValue = None
    has_body: bool = False
    content_type: str | None = None
    warnings: tuple[GenerationWarning, ...] = ()


@dataclass(frozen=True)
class MutationCase:
    """Для параметра path начинается с имени; для тела () обозначает его корень."""

    request: RequestData
    kind: MutationKind
    location: MutationLocation
    path: JSONPath
