"""Разбор поддерживаемого подмножества OpenAPI 3.0/3.1 в модели запросов."""

import json
import math
import re
from dataclasses import replace

import yaml

from rattle.exceptions import InvalidOpenAPISpecError
from rattle.models import (
    EndpointSpec,
    HttpMethod,
    OpenAPISpec,
    ParameterSpec,
    ParamLocation,
    RequestBodySpec,
    SchemaSpec,
)

_METHODS: dict[str, HttpMethod] = {
    "get": "GET",
    "post": "POST",
    "put": "PUT",
    "patch": "PATCH",
    "delete": "DELETE",
}
_LOCATIONS: dict[str, ParamLocation] = {
    "path": "path",
    "query": "query",
    "header": "header",
    "cookie": "cookie",
}
_TYPES = frozenset({"object", "array", "string", "integer", "number", "boolean", "null"})
_ANNOTATIONS = frozenset(
    {
        "title",
        "description",
        "default",
        "example",
        "examples",
        "deprecated",
        "readOnly",
        "writeOnly",
        "xml",
        "externalDocs",
        "$schema",
    }
)
_SCHEMA_KEYS = _ANNOTATIONS | {
    "$ref",
    "type",
    "nullable",
    "enum",
    "minimum",
    "maximum",
    "minLength",
    "maxLength",
    "pattern",
    "format",
    "required",
    "properties",
    "items",
    "anyOf",
    "allOf",
}
_PATH_METADATA = frozenset({"summary", "description", "servers", "parameters"})
_MAX_DEPTH = 64
_MAX_SCHEMA_NODES = 10_000


class OpenAPIParser:
    """Переиспользуемый парсер: состояние документа не хранится между вызовами."""

    def parse(self, raw_spec: str | bytes) -> OpenAPISpec:
        """Читает JSON/YAML; ошибки входа оборачивает в InvalidOpenAPISpecError."""
        try:
            text = raw_spec.decode("utf-8-sig") if isinstance(raw_spec, bytes) else raw_spec
        except UnicodeDecodeError as error:
            raise InvalidOpenAPISpecError("Не удалось декодировать схему как UTF-8") from error
        text = text.lstrip("\ufeff")
        if not text.strip():
            raise InvalidOpenAPISpecError("Пустая OpenAPI-схема")

        try:
            document: object
            try:
                document = json.loads(text)
            except json.JSONDecodeError:
                document = yaml.safe_load(text)
            return _DocumentParser(_mapping(document, "Корень OpenAPI-схемы")).parse()
        except (yaml.YAMLError, ValueError) as error:
            raise InvalidOpenAPISpecError("Некорректный JSON или YAML в OpenAPI-схеме") from error
        except RecursionError as error:
            raise InvalidOpenAPISpecError("Превышена допустимая глубина документа") from error


class _DocumentParser:
    """Контекст одного разбора: корень для $ref, предупреждения и бюджет обхода."""

    def __init__(self, root: dict[str, object]) -> None:
        self._root = root
        self._warnings: list[str] = []
        self._schema_nodes = 0

    def parse(self) -> OpenAPISpec:
        version = self._root.get("openapi")
        if not isinstance(version, str) or re.fullmatch(r"3\.[01]\.\d+", version) is None:
            raise InvalidOpenAPISpecError("Поддерживаются схемы версии OpenAPI 3.0.x и 3.1.x")
        info = _mapping(self._root.get("info", {}), "Раздел info")
        title = _text(info, "title") or ""
        paths = _mapping(self._root.get("paths"), "Раздел paths")
        endpoints: list[EndpointSpec] = []
        for path, raw_item in paths.items():
            if path.startswith("x-"):
                continue
            if not path.startswith("/"):
                raise InvalidOpenAPISpecError(f"Некорректный путь в paths: {path!r}")
            item, _ = self._resolve(_mapping(raw_item, f"Путь {path}"))
            assert item is not None
            inherited = self._parameters(item.get("parameters"))
            for name, raw_operation in item.items():
                method = _METHODS.get(name.lower())
                if method is None:
                    if name.lower() in {"head", "options", "trace"}:
                        self._warnings.append(f"Пропущена операция {name.upper()} {path}")
                    elif name not in _PATH_METADATA and not name.startswith("x-"):
                        raise InvalidOpenAPISpecError(f"Неизвестное поле пути {path}: {name}")
                    continue
                operation = _mapping(raw_operation, f"Операция {method} {path}")
                parameters = self._merge_parameters(
                    inherited, self._parameters(operation.get("parameters"))
                )
                expected = set(re.findall(r"\{([^{}]+)\}", path))
                actual = {p.name for p in parameters if p.location == "path"}
                if expected != actual:
                    raise InvalidOpenAPISpecError(f"Параметры пути не соответствуют шаблону {path}")
                endpoints.append(
                    EndpointSpec(
                        method=method,
                        path=path,
                        parameters=parameters,
                        request_body=self._body(operation.get("requestBody"), f"{method} {path}"),
                        operation_id=_text(operation, "operationId"),
                    )
                )
        if not endpoints:
            raise InvalidOpenAPISpecError("В OpenAPI-схеме не найдено ни одного эндпоинта")
        return OpenAPISpec(
            version=version,
            title=title,
            endpoints=tuple(endpoints),
            warnings=tuple(self._warnings),
        )

    def _parameters(self, raw: object) -> list[ParameterSpec]:
        if raw is None:
            return []
        if not isinstance(raw, list):
            raise InvalidOpenAPISpecError("Раздел parameters должен быть списком")
        result: list[ParameterSpec] = []
        seen: set[tuple[str, ParamLocation]] = set()
        for value in raw:
            parameter, refs = self._resolve(_mapping(value, "Параметр"))
            assert parameter is not None
            name = _text(parameter, "name")
            location = parameter.get("in")
            if not name:
                raise InvalidOpenAPISpecError("У параметра отсутствует строковое имя")
            if not isinstance(location, str) or location not in _LOCATIONS:
                raise InvalidOpenAPISpecError(
                    f"Неподдерживаемое расположение параметра: {location!r}"
                )
            if location == "header" and name.lower() in {"accept", "content-type", "authorization"}:
                self._warnings.append(f"Пропущен зарезервированный header-параметр {name}")
                continue
            required = _boolean(parameter, "required", default=location == "path")
            if location == "path" and not required:
                raise InvalidOpenAPISpecError("У path-параметра required должен быть true")
            if "content" in parameter:
                raise InvalidOpenAPISpecError("Параметры через content пока не поддерживаются")
            parsed = ParameterSpec(
                name=name,
                location=_LOCATIONS[location],
                required=required,
                schema=self._schema(parameter.get("schema", {}), refs),
            )
            key = _parameter_key(parsed)
            if key in seen:
                raise InvalidOpenAPISpecError(f"Повторяющийся параметр: {name}")
            seen.add(key)
            result.append(parsed)
        return result

    @staticmethod
    def _merge_parameters(
        inherited: list[ParameterSpec], own: list[ParameterSpec]
    ) -> tuple[ParameterSpec, ...]:
        merged = {_parameter_key(parameter): parameter for parameter in inherited}
        for parameter in own:
            merged[_parameter_key(parameter)] = parameter
        return tuple(merged.values())

    def _body(self, raw: object, operation: str) -> RequestBodySpec | None:
        if raw is None:
            return None
        body, refs = self._resolve(_mapping(raw, "Раздел requestBody"))
        assert body is not None
        required = _boolean(body, "required")
        content = _mapping(body.get("content"), "Раздел requestBody.content")
        # Обычный JSON предпочтительнее специфичных application/*+json.
        for exact in (True, False):
            for media_type, value in content.items():
                base_type = media_type.split(";", 1)[0].strip().lower()
                if (exact and base_type == "application/json") or (
                    not exact
                    and base_type.startswith("application/")
                    and base_type.endswith("+json")
                ):
                    media = _mapping(value, f"Описание {media_type}")
                    return RequestBodySpec(
                        required=required,
                        content_type=media_type,
                        schema=self._schema(media.get("schema", {}), refs),
                    )
        self._warnings.append(
            f"{operation}: пропущено не-JSON тело ({', '.join(content) or 'пустой content'})"
        )
        return None

    def _schema(
        self, raw: object, active_refs: frozenset[str] = frozenset(), depth: int = 0
    ) -> SchemaSpec:
        if depth > _MAX_DEPTH:
            raise InvalidOpenAPISpecError("Превышена допустимая глубина схемы")
        self._schema_nodes += 1
        if self._schema_nodes > _MAX_SCHEMA_NODES:
            raise InvalidOpenAPISpecError("Превышен лимит раскрытия схем")
        node, refs = self._resolve(_mapping(raw, "Описание схемы"), active_refs)
        if node is None:
            return SchemaSpec(is_recursive=True)
        unsupported = {key for key in node if key not in _SCHEMA_KEYS and not key.startswith("x-")}
        if unsupported:
            raise InvalidOpenAPISpecError(
                f"Неподдерживаемые поля схемы: {', '.join(sorted(unsupported))}"
            )
        nullable = _boolean(node, "nullable")
        composed = self._composition(node, refs, depth)
        if composed is not None:
            return replace(composed, nullable=nullable or composed.nullable)
        schema_type, null_type = _schema_type(node)
        properties = {
            name: self._schema(value, refs, depth + 1)
            for name, value in _mapping(node.get("properties", {}), "Раздел properties").items()
        }
        items = self._schema(node["items"], refs, depth + 1) if "items" in node else None
        if schema_type == "any":
            if properties:
                schema_type = "object"
            elif items is not None:
                schema_type = "array"
        enum: tuple[object, ...] = ()
        if "enum" in node:
            raw_enum = node["enum"]
            if not isinstance(raw_enum, list) or not raw_enum:
                raise InvalidOpenAPISpecError("enum должен быть непустым списком")
            enum = tuple(raw_enum)
        raw_required = node.get("required", [])
        if not isinstance(raw_required, list):
            raise InvalidOpenAPISpecError("required должен быть списком имён полей")
        required: set[str] = set()
        for name in raw_required:
            if not isinstance(name, str):
                raise InvalidOpenAPISpecError("Имена в required должны быть строками")
            required.add(name)
        minimum, maximum = _number(node, "minimum"), _number(node, "maximum")
        min_length, max_length = _length(node, "minLength"), _length(node, "maxLength")
        if minimum is not None and maximum is not None and minimum > maximum:
            raise InvalidOpenAPISpecError("minimum не может быть больше maximum")
        if min_length is not None and max_length is not None and min_length > max_length:
            raise InvalidOpenAPISpecError("minLength не может быть больше maxLength")
        return SchemaSpec(
            schema_type=schema_type,
            nullable=nullable or null_type,
            enum=enum,
            minimum=minimum,
            maximum=maximum,
            min_length=min_length,
            max_length=max_length,
            pattern=_text(node, "pattern"),
            fmt=_text(node, "format"),
            required_fields=frozenset(required),
            properties=properties,
            items=items,
        )

    def _composition(
        self, node: dict[str, object], refs: frozenset[str], depth: int
    ) -> SchemaSpec | None:
        keywords = node.keys() & {"anyOf", "allOf"}
        if not keywords:
            return None
        if len(keywords) != 1:
            raise InvalidOpenAPISpecError("Сочетание anyOf и allOf не поддерживается")
        keyword = next(iter(keywords))
        siblings = {
            key
            for key in node
            if key not in _ANNOTATIONS
            and key not in {keyword, "nullable"}
            and not key.startswith("x-")
        }
        if siblings:
            raise InvalidOpenAPISpecError(
                f"Не поддерживаются поля рядом с {keyword}: {', '.join(sorted(siblings))}"
            )
        branches = node[keyword]
        if not isinstance(branches, list):
            raise InvalidOpenAPISpecError(f"{keyword} должен быть списком")
        if keyword == "allOf" and len(branches) == 1:
            return self._schema(branches[0], refs, depth + 1)
        if keyword == "anyOf" and len(branches) == 2:
            variants = [_mapping(branch, "Вариант anyOf") for branch in branches]
            nulls = [branch for branch in variants if branch.get("type") == "null"]
            others = [branch for branch in variants if branch.get("type") != "null"]
            if len(nulls) == 1 and len(others) == 1 and nulls[0].keys() <= _ANNOTATIONS | {"type"}:
                inner = self._schema(others[0], refs, depth + 1)
                return replace(inner, nullable=True)
        raise InvalidOpenAPISpecError(f"Неподдерживаемая конструкция {keyword}")

    def _resolve(
        self, node: dict[str, object], active_refs: frozenset[str] = frozenset()
    ) -> tuple[dict[str, object] | None, frozenset[str]]:
        """None означает рекурсивное поле; цикл чистых алиасов является ошибкой."""
        chain: set[str] = set()
        current = node
        next_refs = active_refs
        while "$ref" in current:
            reference = current["$ref"]
            if not isinstance(reference, str):
                raise InvalidOpenAPISpecError("Значение $ref должно быть строкой")
            siblings = {
                key
                for key in current
                if key != "$ref" and key not in _ANNOTATIONS and not key.startswith("x-")
            }
            if siblings:
                raise InvalidOpenAPISpecError(
                    f"Не поддерживаются поля рядом с $ref: {', '.join(sorted(siblings))}"
                )
            if reference in chain:
                raise InvalidOpenAPISpecError(f"Циклическая ссылка $ref: {reference}")
            if reference in active_refs:
                return None, next_refs
            chain.add(reference)
            if len(chain) > _MAX_DEPTH:
                raise InvalidOpenAPISpecError("Превышен лимит цепочки $ref")
            next_refs = next_refs | {reference}
            current = self._pointer(reference)
        return current, next_refs

    def _pointer(self, reference: str) -> dict[str, object]:
        if not reference.startswith("#/"):
            raise InvalidOpenAPISpecError(
                f"Поддерживаются только локальные ссылки $ref: {reference}"
            )
        current: object = self._root
        for token in reference[2:].split("/"):
            if re.search(r"~(?![01])", token):
                raise InvalidOpenAPISpecError(f"Некорректный JSON Pointer: {reference}")
            key = token.replace("~1", "/").replace("~0", "~")
            mapping = _mapping(current, f"Узел ссылки {reference}")
            if key not in mapping:
                raise InvalidOpenAPISpecError(f"Не найдена ссылка $ref: {reference}")
            current = mapping[key]
        return _mapping(current, f"Цель ссылки {reference}")


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise InvalidOpenAPISpecError(f"{label} должен быть объектом")
    result: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise InvalidOpenAPISpecError(f"{label}: ключи должны быть строками")
        result[key] = item
    return result


def _text(node: dict[str, object], key: str) -> str | None:
    if key not in node:
        return None
    value = node[key]
    if not isinstance(value, str):
        raise InvalidOpenAPISpecError(f"{key} должен быть строкой")
    return value


def _boolean(node: dict[str, object], key: str, *, default: bool = False) -> bool:
    value = node.get(key, default)
    if not isinstance(value, bool):
        raise InvalidOpenAPISpecError(f"{key} должен быть boolean")
    return value


def _number(node: dict[str, object], key: str) -> int | float | None:
    if key not in node:
        return None
    value = node[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidOpenAPISpecError(f"{key} должен быть числом")
    if isinstance(value, float) and not math.isfinite(value):
        raise InvalidOpenAPISpecError(f"{key} должен быть конечным числом")
    return value


def _length(node: dict[str, object], key: str) -> int | None:
    if key not in node:
        return None
    value = node[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise InvalidOpenAPISpecError(f"{key} должен быть неотрицательным целым числом")
    return value


def _schema_type(node: dict[str, object]) -> tuple[str, bool]:
    if "type" not in node:
        return "any", False
    value = node["type"]
    if isinstance(value, str) and value in _TYPES:
        return value, value == "null"
    if isinstance(value, list) and len(value) == 2 and "null" in value:
        others = [item for item in value if isinstance(item, str) and item != "null"]
        if len(others) == 1 and others[0] in _TYPES:
            return others[0], True
    raise InvalidOpenAPISpecError(f"Неподдерживаемое значение type: {value!r}")


def _parameter_key(parameter: ParameterSpec) -> tuple[str, ParamLocation]:
    name = parameter.name.lower() if parameter.location == "header" else parameter.name
    return name, parameter.location
