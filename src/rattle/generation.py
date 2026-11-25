"""Построение данных запросов без HTTP-сериализации и сетевых вызовов."""

import re
from collections.abc import Iterator

from rattle._mutations import request_mutations
from rattle._schema_values import SchemaValues, ValueLimits
from rattle.exceptions import (
    GenerationLimitError,
    NoExampleError,
    RequestGenerationError,
    UnsupportedSchemaError,
)
from rattle.models import EndpointSpec, ParameterSpec, ParamLocation
from rattle.requests import (
    GenerationWarning,
    JSONValue,
    MutationCase,
    RequestData,
    RequestParameter,
)

_STYLES: dict[ParamLocation, str] = {
    "path": "simple",
    "query": "form",
    "header": "simple",
    "cookie": "form",
}


class RequestGenerator:
    """Переиспользуемая конфигурация; контекст и бюджеты создаются на каждый вызов."""

    def __init__(
        self, *, max_depth: int = 32, max_nodes: int = 100_000, max_string_length: int = 4096
    ) -> None:
        self._limits = ValueLimits(max_depth, max_nodes, max_string_length)

    def build_base(self, endpoint: EndpointSpec) -> RequestData:
        """Строит пример; причины необязательных пропусков возвращает в request.warnings."""
        try:
            return self._base(endpoint, SchemaValues(self._limits))
        except RecursionError as error:
            raise GenerationLimitError(
                f"{endpoint.method} {endpoint.path}: Превышена глубина стека генерации"
            ) from error
        except RequestGenerationError as error:
            raise type(error)(f"{endpoint.method} {endpoint.path}: {error}") from error

    def generate(self, endpoint: EndpointSpec, *, limit: int = 100) -> Iterator[MutationCase]:
        """Лениво выдаёт не более limit независимых запросов с одной локальной мутацией.

        Лимит проверяется сразу; схема и базовый запрос — при первом next().
        При limit=0 схема не обходится. Все варианты строятся из собственной базы,
        а не из ранее выданного варианта; сетевых вызовов нет.
        Для диагностики базы, в том числе при пустом итераторе, есть build_base().
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise ValueError("limit должен быть неотрицательным целым числом")
        return self._generate(endpoint, limit)

    def _generate(self, endpoint: EndpointSpec, limit: int) -> Iterator[MutationCase]:
        if limit == 0:
            return
        values = SchemaValues(self._limits)
        try:
            base = self._base(endpoint, values)
            count = 0
            for case in request_mutations(endpoint, base, values):
                yield case
                count += 1
                if count == limit:
                    return
        except RecursionError as error:
            raise GenerationLimitError(
                f"{endpoint.method} {endpoint.path}: Превышена глубина стека генерации"
            ) from error
        except RequestGenerationError as error:
            raise type(error)(f"{endpoint.method} {endpoint.path}: {error}") from error

    def _base(self, endpoint: EndpointSpec, values: SchemaValues) -> RequestData:
        if endpoint.method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            raise UnsupportedSchemaError("Неподдерживаемый HTTP-метод")
        if not endpoint.path.startswith("/"):
            raise UnsupportedSchemaError("Путь должен начинаться с /")
        names = {p.name for p in endpoint.parameters if p.location == "path"}
        if names != set(re.findall(r"\{([^{}]+)\}", endpoint.path)):
            raise UnsupportedSchemaError("Параметры пути не соответствуют шаблону")
        seen: set[tuple[ParamLocation, str]] = set()
        parameters: list[RequestParameter] = []
        warnings: list[GenerationWarning] = []
        for parameter in endpoint.parameters:
            values.step()
            key = _key(parameter.name, parameter.location)
            if key in seen:
                raise UnsupportedSchemaError(f"Повторяющийся параметр: {parameter.name}")
            seen.add(key)
            style, explode = _serialization(parameter)
            try:
                value = values.build(parameter.schema)
            except NoExampleError as error:
                if parameter.required or parameter.location == "path":
                    raise
                warnings.append(
                    GenerationWarning(
                        "omitted_optional",
                        parameter.location,
                        (parameter.name,),
                        f"Необязательный параметр пропущен: {error}",
                    )
                )
                continue
            warnings.extend(
                GenerationWarning(
                    warning.kind,
                    parameter.location,
                    (parameter.name, *warning.path),
                    warning.message,
                )
                for warning in values.warnings
            )
            parameters.append(
                RequestParameter(
                    parameter.name,
                    parameter.location,
                    value,
                    style,
                    explode,
                    parameter.allow_reserved,
                )
            )
        body: JSONValue = None
        has_body = False
        content_type = None
        if endpoint.request_body is not None:
            definition = endpoint.request_body
            media_type = definition.content_type.split(";", 1)[0].strip().lower()
            if media_type != "application/json" and not (
                media_type.startswith("application/") and media_type.endswith("+json")
            ):
                raise UnsupportedSchemaError("Поддерживается только JSON-тело")
            try:
                body = values.build(definition.schema)
            except NoExampleError as error:
                if definition.required:
                    raise
                warnings.append(
                    GenerationWarning(
                        "omitted_optional", "body", (), f"Необязательное тело пропущено: {error}"
                    )
                )
            else:
                has_body = True
                content_type = definition.content_type
                warnings.extend(
                    GenerationWarning(warning.kind, "body", warning.path, warning.message)
                    for warning in values.warnings
                )
        return RequestData(
            endpoint.method,
            endpoint.path,
            tuple(parameters),
            body,
            has_body,
            content_type,
            warnings=tuple(warnings),
        )


def _key(name: str, location: ParamLocation) -> tuple[ParamLocation, str]:
    return location, name.lower() if location == "header" else name


def _serialization(parameter: ParameterSpec) -> tuple[str, bool]:
    style = _STYLES.get(parameter.location)
    if style is None or parameter.style is not None and parameter.style != style:
        raise UnsupportedSchemaError("Неподдерживаемый style параметра")
    if parameter.explode is not None and not isinstance(parameter.explode, bool):
        raise UnsupportedSchemaError("explode должен быть булевым")
    if not isinstance(parameter.allow_reserved, bool) or (
        parameter.allow_reserved and parameter.location != "query"
    ):
        raise UnsupportedSchemaError("allowReserved=true допустим только для query")
    if parameter.location == "path" and not parameter.required:
        raise UnsupportedSchemaError("Path-параметр должен быть обязательным")
    if parameter.location == "header" and parameter.name.lower() in {
        "accept",
        "content-type",
        "authorization",
    }:
        raise UnsupportedSchemaError("Зарезервированный header-параметр не поддерживается")
    explode = parameter.explode if parameter.explode is not None else style == "form"
    return style, explode
