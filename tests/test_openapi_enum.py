import json
from textwrap import indent

import pytest
import yaml

from rattle.exceptions import InvalidOpenAPISpecError
from rattle.models import SchemaSpec
from rattle.openapi import OpenAPIParser


def _document(schema: object) -> dict[str, object]:
    return {
        "openapi": "3.1.0",
        "paths": {
            "/items": {
                "post": {"requestBody": {"content": {"application/json": {"schema": schema}}}}
            }
        },
    }


def _parse_body(parser: OpenAPIParser, raw: str) -> SchemaSpec:
    body = parser.parse(raw).endpoints[0].request_body
    assert body is not None
    return body.schema


def _yaml_with_schema(schema: str) -> str:
    prefix = yaml.safe_dump(_document("SCHEMA"), sort_keys=False)
    return prefix.replace("schema: SCHEMA", "schema:\n" + indent(schema, " " * 14).rstrip())


def test_yaml_aliases_do_not_share_enum_values_between_schemas(parser: OpenAPIParser) -> None:
    schema = _parse_body(
        parser,
        _yaml_with_schema(
            "type: object\n"
            "properties:\n"
            "  first:\n"
            "    enum: &choices [{nested: [original]}]\n"
            "  second:\n"
            "    enum: *choices\n"
        ),
    )
    first = schema.properties["first"].enum[0]
    second = schema.properties["second"].enum[0]
    assert isinstance(first, dict) and isinstance(second, dict)
    assert first is not second
    assert first["nested"] is not second["nested"]

    first["nested"][0] = "changed"
    assert second["nested"] == ["original"]


def test_repeated_acyclic_aliases_in_one_enum_are_independent(parser: OpenAPIParser) -> None:
    schema = _parse_body(parser, _yaml_with_schema("enum: [&value {nested: []}, *value]\n"))
    first, second = schema.enum
    assert isinstance(first, dict) and isinstance(second, dict)
    first["nested"].append(1)
    assert second["nested"] == []


@pytest.mark.parametrize(
    "schema",
    [
        "enum: [&value {self: *value}]\n",
        "enum: [&value [*value]]\n",
        "enum: &values [*values]\n",
    ],
)
def test_cycles_inside_enum_are_spec_errors(parser: OpenAPIParser, schema: str) -> None:
    with pytest.raises(InvalidOpenAPISpecError, match="Циклическое значение enum"):
        parser.parse(_yaml_with_schema(schema))


def test_enum_preserves_json_types_and_nested_values(parser: OpenAPIParser) -> None:
    values: list[object] = [True, 1, None, {"nested": [False, "text", 2.5]}, 2**100]
    schema = _parse_body(parser, json.dumps(_document({"enum": values})))

    assert type(schema.enum[0]) is bool
    assert type(schema.enum[1]) is int
    assert json.loads(json.dumps(schema.enum, allow_nan=False)) == values


@pytest.mark.parametrize("levels", [64, 65])
def test_enum_depth_is_bounded(parser: OpenAPIParser, levels: int) -> None:
    value: object = "leaf"
    for _ in range(levels):
        value = [value]
    raw = json.dumps(_document({"enum": [value]}))

    if levels == 65:
        with pytest.raises(InvalidOpenAPISpecError, match="глубин.*enum"):
            parser.parse(raw)
    else:
        assert _parse_body(parser, raw).enum


def test_enum_alias_expansion_has_a_finite_budget(parser: OpenAPIParser) -> None:
    value: object = "leaf"
    for _ in range(14):
        value = [value, value]
    raw = yaml.safe_dump(_document({"enum": [value]}))

    with pytest.raises(InvalidOpenAPISpecError, match="лимит.*enum"):
        parser.parse(raw)


def test_enum_budget_is_shared_by_schemas_but_not_documents(parser: OpenAPIParser) -> None:
    values = [None] * 6000
    raw = json.dumps(
        _document({"properties": {"first": {"enum": values}, "second": {"enum": values}}})
    )
    with pytest.raises(InvalidOpenAPISpecError, match="лимит.*enum"):
        parser.parse(raw)

    raw = json.dumps(_document({"enum": values}))
    assert len(_parse_body(parser, raw).enum) == 6000
    assert len(_parse_body(parser, raw).enum) == 6000


def test_parser_recovers_after_an_enum_cycle(parser: OpenAPIParser) -> None:
    with pytest.raises(InvalidOpenAPISpecError):
        parser.parse(_yaml_with_schema("enum: [&value [*value]]\n"))

    assert _parse_body(parser, json.dumps(_document({"enum": ["ok"]}))).enum == ("ok",)
