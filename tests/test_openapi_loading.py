import json
from textwrap import indent

import pytest
import yaml

from rattle.exceptions import InvalidOpenAPISpecError
from rattle.openapi import OpenAPIParser


def _yaml_with_schema(schema: str) -> str:
    return (
        'openapi: "3.1.0"\n'
        "paths:\n"
        "  /items:\n"
        "    post:\n"
        "      requestBody:\n"
        "        content:\n"
        "          application/json:\n"
        "            schema:\n" + indent(schema, " " * 14)
    )


@pytest.mark.parametrize(
    ("literal", "expected"),
    [
        ("on", "on"),
        ("off", "off"),
        ("yes", "yes"),
        ("NO", "NO"),
        ("2024-01-01", "2024-01-01"),
        ("1:20", "1:20"),
        ("012", 12),
        ("0o12", 10),
        ("0x10", 16),
        ("1e3", 1000.0),
        (".5", 0.5),
        ("-2.5e-2", -0.025),
        ("true", True),
        ("False", False),
        ("null", None),
        ("~", None),
        ("'true'", "true"),
        ('"012"', "012"),
    ],
)
def test_yaml_scalars_use_json_compatible_yaml_12_types(
    parser: OpenAPIParser, literal: str, expected: object
) -> None:
    result = parser.parse(_yaml_with_schema(f"enum: [{literal}]\n"))
    body = result.endpoints[0].request_body
    assert body is not None
    value = body.schema.enum[0]

    assert type(value) is type(expected)
    assert value == expected
    json.dumps(value, allow_nan=False)


def test_parser_does_not_modify_pyyaml_global_resolvers(parser: OpenAPIParser) -> None:
    assert yaml.safe_load("on") is True
    parser.parse(_yaml_with_schema("enum: [on]\n"))
    assert yaml.safe_load("on") is True


@pytest.mark.parametrize(
    "raw",
    [
        '{"openapi":"3.1.0","paths":{"/x":{"get":{}}},"info":{"title":"a","title":"b"}}',
        '{"openapi":"3.1.0","paths":{"/x":{"get":{}}},"info":{"title":"a","ti\\u0074le":"b"}}',
        _yaml_with_schema("type: string\nminLength: 1\nminLength: 5\n"),
        'openapi: "3.1.0"\npaths: {/x: {get: {}}}\nx-data: {key: 1, key: 2}\n',
    ],
)
def test_duplicate_keys_are_rejected_even_in_ignored_sections(
    parser: OpenAPIParser, raw: str
) -> None:
    with pytest.raises(InvalidOpenAPISpecError, match="Повторяющийся ключ"):
        parser.parse(raw)


@pytest.mark.parametrize(
    "literal",
    [
        "!!timestamp 2024-01-01",
        "!!binary dGVzdA==",
        "!!set {key: null}",
        "!!bool on",
        "!!int '1:20'",
        "!!float '1:20'",
        "!!null not-null",
    ],
)
def test_non_json_tags_and_legacy_explicit_scalars_are_rejected(
    parser: OpenAPIParser, literal: str
) -> None:
    with pytest.raises(InvalidOpenAPISpecError):
        parser.parse(_yaml_with_schema(f"enum: [{literal}]\n"))


@pytest.mark.parametrize("literal", [".nan", ".inf", "-.Inf", "1e999"])
def test_non_finite_yaml_numbers_are_spec_errors(parser: OpenAPIParser, literal: str) -> None:
    with pytest.raises(InvalidOpenAPISpecError):
        parser.parse(_yaml_with_schema(f"enum: [{literal}]\n"))


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity", "1e999"])
def test_non_finite_json_numbers_are_not_retried_as_yaml(
    parser: OpenAPIParser, literal: str
) -> None:
    raw = json.dumps({"openapi": "3.1.0", "paths": {"/x": {"get": {}}}, "x-value": "NUMBER"})
    with pytest.raises(InvalidOpenAPISpecError):
        parser.parse(raw.replace('"NUMBER"', literal))


def test_yaml_mapping_keys_must_be_strings(parser: OpenAPIParser) -> None:
    raw = 'openapi: "3.1.0"\npaths: {/x: {get: {}}}\nx-data: {1: value}\n'
    with pytest.raises(InvalidOpenAPISpecError, match="ключи.*строк"):
        parser.parse(raw)


def test_parser_recovers_after_a_duplicate_key_error(parser: OpenAPIParser) -> None:
    with pytest.raises(InvalidOpenAPISpecError):
        parser.parse(_yaml_with_schema("type: string\ntype: integer\n"))

    result = parser.parse(_yaml_with_schema("type: string\n"))
    assert result.endpoints[0].request_body is not None
    assert result.warnings == ()
