import json
from urllib.parse import quote

import pytest

from rattle.exceptions import InvalidOpenAPISpecError
from rattle.models import SchemaSpec
from rattle.openapi import OpenAPIParser


def _with_reference(reference: str, **extra: object) -> str:
    return json.dumps(
        {
            "openapi": "3.1.0",
            "paths": {
                "/items": {
                    "post": {
                        "requestBody": {
                            "content": {"application/json": {"schema": {"$ref": reference}}}
                        }
                    }
                }
            },
            **extra,
        }
    )


def _body(parser: OpenAPIParser, raw: str) -> SchemaSpec:
    body = parser.parse(raw).endpoints[0].request_body
    assert body is not None
    return body.schema


@pytest.mark.parametrize("name", ["A B", "A+B", "A%20B", "Пример", "A/B~1"])
def test_uri_fragment_decoding_happens_once_before_pointer_unescaping(
    parser: OpenAPIParser, name: str
) -> None:
    token = name.replace("~", "~0").replace("/", "~1")
    reference = "#/components/schemas/" + quote(token, safe="")
    raw = _with_reference(reference, components={"schemas": {name: {"type": "string"}}})
    assert _body(parser, raw).schema_type == "string"


def test_literal_plus_is_not_decoded_as_a_space(parser: OpenAPIParser) -> None:
    raw = _with_reference(
        "#/components/schemas/A+B",
        components={"schemas": {"A+B": {"type": "integer"}, "A B": {"type": "string"}}},
    )
    assert _body(parser, raw).schema_type == "integer"


def test_percent_encoded_separators_are_decoded_before_splitting(parser: OpenAPIParser) -> None:
    raw = _with_reference(
        "#%2Fcomponents%2Fschemas%2FA%7E1B",
        components={"schemas": {"A/B": {"type": "boolean"}}},
    )
    assert _body(parser, raw).schema_type == "boolean"


@pytest.mark.parametrize("index", ["0", "%30"])
def test_pointer_can_traverse_arrays(parser: OpenAPIParser, index: str) -> None:
    raw = _with_reference(
        f"#/x-values/{index}/schema",
        **{"x-values": [{"schema": {"type": "integer"}}]},
    )
    assert _body(parser, raw).schema_type == "integer"


@pytest.mark.parametrize("token", ["00", "01", "-1", "+0", "-", "1", "9" * 5000])
def test_bad_or_missing_array_indices_are_spec_errors(parser: OpenAPIParser, token: str) -> None:
    raw = _with_reference(f"#/x-values/{token}", **{"x-values": [{"type": "integer"}]})
    with pytest.raises(InvalidOpenAPISpecError, match="ссылк|JSON Pointer"):
        parser.parse(raw)


@pytest.mark.parametrize("key", ["00", "-", "0"])
def test_numeric_object_keys_are_not_treated_as_array_indices(
    parser: OpenAPIParser, key: str
) -> None:
    raw = _with_reference(f"#/x-values/{key}", **{"x-values": {key: {"type": "string"}}})
    assert _body(parser, raw).schema_type == "string"


@pytest.mark.parametrize("suffix", ["%", "%2", "%GG", "%FF", "%C3%28", "%7E2"])
def test_malformed_uri_fragments_are_spec_errors(parser: OpenAPIParser, suffix: str) -> None:
    raw = _with_reference("#/components/schemas/" + suffix)
    with pytest.raises(InvalidOpenAPISpecError, match="JSON Pointer|UTF-8"):
        parser.parse(raw)


def test_equivalent_reference_spellings_share_recursion_identity(parser: OpenAPIParser) -> None:
    raw = _with_reference(
        "#/components/schemas/N%20ode",
        components={
            "schemas": {
                "N ode": {
                    "type": "object",
                    "properties": {"next": {"$ref": "#/components/schemas/N ode"}},
                }
            }
        },
    )
    assert _body(parser, raw).properties["next"].is_recursive is True


def test_encoded_reference_alias_cycles_are_still_errors(parser: OpenAPIParser) -> None:
    raw = _with_reference(
        "#/components/schemas/A",
        components={"schemas": {"A": {"$ref": "#/components/schemas/%41"}}},
    )
    with pytest.raises(InvalidOpenAPISpecError, match="Циклическая ссылка"):
        parser.parse(raw)


@pytest.mark.parametrize("target", [[{"type": "string"}], "text", 1])
def test_reference_target_must_still_be_an_object(parser: OpenAPIParser, target: object) -> None:
    with pytest.raises(InvalidOpenAPISpecError, match="объект"):
        parser.parse(_with_reference("#/x-value", **{"x-value": target}))


def test_parser_recovers_after_malformed_pointer(parser: OpenAPIParser) -> None:
    with pytest.raises(InvalidOpenAPISpecError):
        parser.parse(_with_reference("#/x-value/%GG"))
    raw = _with_reference("#/x-value/0", **{"x-value": [{"type": "string"}]})
    assert _body(parser, raw).schema_type == "string"
