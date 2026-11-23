"""Модели разобранной OpenAPI-схемы.

frozen защищает атрибуты, но не вложенные dict/list. Стандартное равенство
не является семантическим сравнением JSON Schema; хешируемость не обещается.
"""

from dataclasses import dataclass, field
from typing import Literal

ParamLocation = Literal["path", "query", "header", "cookie"]
HttpMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]


@dataclass(frozen=True)
class SchemaSpec:
    """Описание типа данных и ограничений поля или тела запроса."""

    schema_type: str = "any"
    nullable: bool = False
    enum: tuple[object, ...] = ()
    minimum: int | float | None = None
    maximum: int | float | None = None
    min_length: int | None = None
    max_length: int | None = None
    pattern: str | None = None
    fmt: str | None = None
    required_fields: frozenset[str] = frozenset()
    properties: dict[str, "SchemaSpec"] = field(default_factory=dict)
    items: "SchemaSpec | None" = None
    is_recursive: bool = False


@dataclass(frozen=True)
class ParameterSpec:
    """Параметр запроса; парсер нормализует style/explode по расположению."""

    name: str
    location: ParamLocation
    required: bool
    schema: SchemaSpec
    style: str | None = None
    explode: bool | None = None
    allow_reserved: bool = False


@dataclass(frozen=True)
class RequestBodySpec:
    """Описание JSON-тела запроса."""

    required: bool
    content_type: str
    schema: SchemaSpec


@dataclass(frozen=True)
class EndpointSpec:
    """Описание одного эндпоинта целевого API."""

    method: HttpMethod
    path: str
    parameters: tuple[ParameterSpec, ...] = ()
    request_body: RequestBodySpec | None = None
    operation_id: str | None = None


@dataclass(frozen=True)
class OpenAPISpec:
    """Разобранная OpenAPI-схема целевого API."""

    version: str
    title: str
    endpoints: tuple[EndpointSpec, ...]
    warnings: tuple[str, ...] = ()
