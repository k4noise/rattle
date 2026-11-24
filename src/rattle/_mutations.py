"""Ленивый обход мест мутаций и независимое копирование каждого запроса."""

import math
from collections.abc import Iterator
from dataclasses import dataclass, replace

from rattle._schema_values import SchemaValues
from rattle.models import EndpointSpec, SchemaSpec
from rattle.requests import JSONPath, JSONValue, MutationCase, MutationKind, RequestData


@dataclass(frozen=True)
class _Edit:
    kind: MutationKind
    path: JSONPath = ()
    value: JSONValue = None
    remove: bool = False


def request_mutations(
    endpoint: EndpointSpec, base: RequestData, values: SchemaValues
) -> Iterator[MutationCase]:
    definitions = {(p.location, p.name): p for p in endpoint.parameters}
    for index, parameter in enumerate(base.parameters):
        definition = definitions[parameter.location, parameter.name]
        if definition.required:
            yield _case(base, index, _Edit("missing_required", remove=True), values)
        for edit in _value_edits(definition.schema, parameter.value, values):
            yield _case(base, index, edit, values)
    if base.has_body and endpoint.request_body is not None:
        if endpoint.request_body.required:
            yield _case(base, None, _Edit("missing_required", remove=True), values)
        for edit in _value_edits(endpoint.request_body.schema, base.body, values):
            yield _case(base, None, edit, values)


def _value_edits(
    schema: SchemaSpec,
    value: JSONValue,
    values: SchemaValues,
    path: JSONPath = (),
    depth: int = 0,
) -> Iterator[_Edit]:
    values.step(depth)
    if isinstance(value, dict):
        for name in sorted(schema.required_fields):
            yield _Edit("missing_required", (*path, name), remove=True)
    if schema.schema_type != "any":
        wrong_types: dict[str, JSONValue] = {
            "string": 0,
            "integer": "",
            "number": "",
            "boolean": 0,
            "null": "",
            "object": [],
            "array": {},
        }
        yield _Edit("wrong_type", path, wrong_types[schema.schema_type])
    if schema.enum:
        for candidate in _enum_candidates(schema, values):
            if not values.in_enum(schema, candidate):
                yield _Edit("outside_enum", path, candidate)
                break
    if schema.schema_type in {"any", "integer", "number"}:
        if schema.minimum is not None:
            yield _Edit("below_minimum", path, math.ceil(schema.minimum) - 1)
        if schema.maximum is not None:
            yield _Edit("above_maximum", path, math.floor(schema.maximum) + 1)
    if schema.schema_type in {"any", "string"}:
        if schema.min_length is not None and schema.min_length > 0:
            yield _Edit("too_short", path, values.text(schema.min_length - 1))
        if schema.max_length is not None:
            yield _Edit("too_long", path, values.text(schema.max_length + 1))
    if isinstance(value, dict):
        for name, child in schema.properties.items():
            if name in value:
                yield from _value_edits(child, value[name], values, (*path, name), depth + 1)
    if isinstance(value, list) and schema.items is not None:
        for index, item in enumerate(value):
            yield from _value_edits(schema.items, item, values, (*path, index), depth + 1)


def _enum_candidates(schema: SchemaSpec, values: SchemaValues) -> Iterator[JSONValue]:
    """Кандидаты сохраняют тип, но могут нарушить другие пересекающиеся ограничения."""
    if values.allows_type(schema, None):
        yield None
    kind = schema.schema_type
    if kind == "null":
        return
    if kind in {"boolean", "any"}:
        yield False
        yield True
        if kind == "boolean":
            return
    if kind == "string":
        yield ""
    elif kind == "array":
        yield []
    elif kind == "object":
        yield {}
    # В бесконечном типе N + 1 различных значений не могут все входить в enum длины N.
    for index in range(len(schema.enum) + 1):
        values.step()
        if kind == "string":
            text = str(index)
            values.check_string(text)
            yield text
        elif kind == "array":
            yield [index]
        elif kind == "object":
            yield {"": index}
        else:
            yield index


def _case(base: RequestData, index: int | None, edit: _Edit, values: SchemaValues) -> MutationCase:
    parameters = []
    for position, parameter in enumerate(base.parameters):
        if position == index:
            if edit.remove and not edit.path:
                continue
            value = _apply(parameter.value, edit, values)
        else:
            value = values.clone(parameter.value)
        parameters.append(replace(parameter, value=value))
    has_body, content_type = base.has_body, base.content_type
    if index is None:
        if edit.remove and not edit.path:
            body: JSONValue = None
            has_body, content_type = False, None
        else:
            body = _apply(base.body, edit, values)
    else:
        body = values.clone(base.body) if has_body else None
    request = replace(
        base, parameters=tuple(parameters), body=body, has_body=has_body, content_type=content_type
    )
    if index is None:
        return MutationCase(request, edit.kind, "body", edit.path)
    parameter = base.parameters[index]
    return MutationCase(request, edit.kind, parameter.location, (parameter.name, *edit.path))


def _apply(value: JSONValue, edit: _Edit, values: SchemaValues) -> JSONValue:
    if not edit.path:
        return values.clone(edit.value)
    result = values.clone(value)
    parent = result
    for key in edit.path[:-1]:
        if isinstance(key, str):
            assert isinstance(parent, dict)
            parent = parent[key]
        else:
            assert isinstance(parent, list)
            parent = parent[key]
    last = edit.path[-1]
    if isinstance(last, str):
        assert isinstance(parent, dict)
        if edit.remove:
            del parent[last]
        else:
            parent[last] = values.clone(edit.value, depth=len(edit.path))
    else:
        assert isinstance(parent, list)
        if edit.remove:
            del parent[last]
        else:
            parent[last] = values.clone(edit.value, depth=len(edit.path))
    return result
