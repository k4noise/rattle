import json

import pytest

from rattle.exceptions import InvalidOpenAPISpecError
from rattle.models import SchemaSpec
from rattle.openapi import OpenAPIParser


def _parse_schema(parser: OpenAPIParser, schema: object, version: str = "3.1.0") -> SchemaSpec:
    raw = json.dumps(
        {
            "openapi": version,
            "paths": {
                "/items": {
                    "post": {"requestBody": {"content": {"application/json": {"schema": schema}}}}
                }
            },
        }
    )
    body = parser.parse(raw).endpoints[0].request_body
    assert body is not None
    return body.schema


@pytest.mark.parametrize("version", ["3.0.3", "3.1.0"])
@pytest.mark.parametrize(
    "schema",
    [
        {"properties": {"name": {"type": "string"}}},
        {"properties": {}},
        {"items": {"type": "integer"}},
        {"properties": {"name": {}}, "items": {}},
        {"required": ["name"], "minimum": 1, "minLength": 2},
    ],
)
def test_missing_type_stays_unspecified(
    parser: OpenAPIParser, version: str, schema: object
) -> None:
    assert _parse_schema(parser, schema, version).schema_type == "any"


def test_constraints_are_retained_without_an_explicit_type(parser: OpenAPIParser) -> None:
    schema = _parse_schema(
        parser,
        {
            "nullable": True,
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
            "items": {"type": "integer"},
        },
    )

    assert schema.schema_type == "any"
    assert schema.nullable is True
    assert schema.properties["name"].schema_type == "string"
    assert schema.required_fields == frozenset({"name"})
    assert schema.items is not None
    assert schema.items.schema_type == "integer"


def test_parser_does_not_rewrite_an_unsatisfiable_enum(parser: OpenAPIParser) -> None:
    schema = _parse_schema(parser, {"type": "integer", "enum": ["abc"]})

    assert schema.schema_type == "integer"
    assert schema.enum == ("abc",)


@pytest.mark.parametrize(
    "schema_type", [None, "string", "number", "integer", "array", "object", "boolean", "null"]
)
def test_inverted_numeric_bounds_are_preserved(
    parser: OpenAPIParser, schema_type: str | None
) -> None:
    raw_schema: dict[str, object] = {"minimum": 10, "maximum": 1}
    if schema_type is not None:
        raw_schema["type"] = schema_type
    schema = _parse_schema(parser, raw_schema)

    assert schema.schema_type == (schema_type or "any")
    assert schema.minimum == 10
    assert schema.maximum == 1


@pytest.mark.parametrize(
    "schema_type", [None, "string", "number", "integer", "array", "object", "boolean", "null"]
)
def test_inverted_string_bounds_are_preserved(
    parser: OpenAPIParser, schema_type: str | None
) -> None:
    raw_schema: dict[str, object] = {"minLength": 5, "maxLength": 2}
    if schema_type is not None:
        raw_schema["type"] = schema_type
    schema = _parse_schema(parser, raw_schema)

    assert schema.schema_type == (schema_type or "any")
    assert schema.min_length == 5
    assert schema.max_length == 2


def test_inverted_numeric_bounds_do_not_remove_a_nullable_alternative(
    parser: OpenAPIParser,
) -> None:
    schema = _parse_schema(parser, {"type": ["integer", "null"], "minimum": 10, "maximum": 1})

    assert schema.schema_type == "integer"
    assert schema.nullable is True
    assert schema.minimum == 10
    assert schema.maximum == 1


@pytest.mark.parametrize(
    ("schema", "message"),
    [
        ({"type": "string", "minimum": True}, "minimum"),
        ({"type": "string", "maximum": "10"}, "maximum"),
        ({"type": "integer", "minLength": -1}, "minLength"),
        ({"maxLength": 1.5}, "maxLength"),
    ],
)
def test_bound_keywords_still_require_well_formed_values(
    parser: OpenAPIParser, schema: object, message: str
) -> None:
    with pytest.raises(InvalidOpenAPISpecError, match=message):
        _parse_schema(parser, schema)
