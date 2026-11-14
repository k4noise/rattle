"""Чтение документа OpenAPI и базовых операций без параметров и тела."""

import json
import re

import yaml

from rattle.exceptions import InvalidOpenAPISpecError
from rattle.models import EndpointSpec, HttpMethod, OpenAPISpec

_METHODS: dict[str, HttpMethod] = {
    "get": "GET",
    "post": "POST",
    "put": "PUT",
    "patch": "PATCH",
    "delete": "DELETE",
}
_PATH_METADATA = frozenset({"summary", "description", "servers", "parameters"})


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
    """Контекст одного разбора: документ и предупреждения."""

    def __init__(self, root: dict[str, object]) -> None:
        self._root = root
        self._warnings: list[str] = []

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
            item = _mapping(raw_item, f"Путь {path}")
            if "$ref" in item or "parameters" in item:
                raise NotImplementedError("Параметры пути и ссылки пока не реализованы")
            for name, raw_operation in item.items():
                method = _METHODS.get(name.lower())
                if method is None:
                    if name.lower() in {"head", "options", "trace"}:
                        self._warnings.append(f"Пропущена операция {name.upper()} {path}")
                    elif name not in _PATH_METADATA and not name.startswith("x-"):
                        raise InvalidOpenAPISpecError(f"Неизвестное поле пути {path}: {name}")
                    continue
                operation = _mapping(raw_operation, f"Операция {method} {path}")
                if "parameters" in operation or "requestBody" in operation:
                    raise NotImplementedError("Параметры и тело запроса пока не реализованы")
                if re.search(r"\{([^{}]+)\}", path):
                    raise NotImplementedError("Параметры пути пока не реализованы")
                endpoints.append(
                    EndpointSpec(
                        method=method, path=path, operation_id=_text(operation, "operationId")
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
