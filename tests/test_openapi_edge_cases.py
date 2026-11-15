import json
import sys

import pytest

from rattle.exceptions import InvalidOpenAPISpecError
from rattle.openapi import OpenAPIParser


def _spec_with_body(schema: object, *, schemas: dict[str, object] | None = None) -> str:
    return json.dumps(
        {
            "openapi": "3.1.0",
            "info": {"title": "Test API", "version": "1.0.0"},
            "paths": {
                "/items": {
                    "post": {
                        "requestBody": {"content": {"application/json": {"schema": schema}}},
                        "responses": {"200": {"description": "OK"}},
                    }
                }
            },
            "components": {"schemas": schemas or {}},
        }
    )


@pytest.mark.parametrize(
    ("schema", "message"),
    [
        pytest.param(
            {"oneOf": [{"type": "string"}, {"type": "integer"}]},
            "oneOf",
            id="one-of-is-not-any",
        ),
        pytest.param(
            {"anyOf": [{"type": "string"}, {"type": "integer"}]},
            "anyOf",
            id="general-any-of",
        ),
        pytest.param(
            {"allOf": [{"type": "string"}, {"maxLength": 10}]},
            "allOf",
            id="multiple-all-of-branches",
        ),
        pytest.param({"type": ["string", "integer"]}, "type", id="multiple-types"),
        pytest.param({"type": "unknown"}, "type", id="unknown-type"),
        pytest.param({"type": []}, "type", id="empty-type-list"),
        pytest.param({"type": "string", "nullable": "false"}, "nullable", id="boolean-string"),
        pytest.param({"type": "string", "minLength": True}, "minLength", id="boolean-length"),
        pytest.param({"type": "string", "minLength": -1}, "minLength", id="negative-length"),
        pytest.param(
            {"type": "string", "minLength": 5, "maxLength": 2},
            "minLength",
            id="inverted-length-bounds",
        ),
        pytest.param({"type": "integer", "minimum": True}, "minimum", id="boolean-number"),
        pytest.param({"type": "number", "minimum": float("nan")}, "minimum", id="nan-bound"),
        pytest.param(
            {"type": "integer", "minimum": 10, "maximum": 1},
            "minimum",
            id="inverted-number-bounds",
        ),
        pytest.param({"type": "object", "properties": {"x": 42}}, "схем", id="bad-property"),
        pytest.param({"type": "object", "required": [1]}, "required", id="bad-required-name"),
        pytest.param({"type": "string", "enum": []}, "enum", id="empty-enum"),
        pytest.param(
            {"type": "object", "additionalProperties": False},
            "additionalProperties",
            id="unsupported-constraint",
        ),
        pytest.param(
            {"allOf": [{"type": "string"}], "maxLength": 3},
            "maxLength",
            id="composition-sibling-is-not-discarded",
        ),
    ],
)
def test_bad_or_unsupported_schemas_are_not_silently_weakened(
    parser: OpenAPIParser, schema: object, message: str
) -> None:
    raw = _spec_with_body(schema, schemas={"Name": {"type": "string"}})

    with pytest.raises(InvalidOpenAPISpecError, match=message):
        parser.parse(raw)


def test_large_integer_bounds_keep_their_precision(parser: OpenAPIParser) -> None:
    bound = 2**100 + 1
    result = parser.parse(_spec_with_body({"type": "integer", "minimum": bound}))
    body = result.endpoints[0].request_body

    assert body is not None
    assert isinstance(body.schema.minimum, int)
    assert body.schema.minimum == bound


def test_header_override_is_case_insensitive(parser: OpenAPIParser) -> None:
    raw = json.dumps(
        {
            "openapi": "3.0.3",
            "paths": {
                "/items": {
                    "parameters": [
                        {"name": "X-Token", "in": "header", "schema": {"type": "string"}}
                    ],
                    "get": {
                        "parameters": [
                            {
                                "name": "x-token",
                                "in": "header",
                                "required": True,
                                "schema": {"type": "integer"},
                            }
                        ]
                    },
                }
            },
        }
    )
    parameters = parser.parse(raw).endpoints[0].parameters

    assert len(parameters) == 1
    assert parameters[0].name == "x-token"
    assert parameters[0].required is True
    assert parameters[0].schema.schema_type == "integer"


def test_parameter_required_must_be_boolean(parser: OpenAPIParser) -> None:
    raw = json.dumps(
        {
            "openapi": "3.0.3",
            "paths": {
                "/items": {
                    "get": {"parameters": [{"name": "q", "in": "query", "required": "false"}]}
                }
            },
        }
    )
    with pytest.raises(InvalidOpenAPISpecError, match="required"):
        parser.parse(raw)


def test_skipped_methods_and_non_json_bodies_are_reported(parser: OpenAPIParser) -> None:
    raw = json.dumps(
        {
            "openapi": "3.0.3",
            "paths": {
                "/upload": {
                    "head": {},
                    "post": {
                        "requestBody": {
                            "content": {"multipart/form-data": {"schema": {"type": "object"}}}
                        }
                    },
                }
            },
        }
    )
    result = parser.parse(raw)

    assert len(result.endpoints) == 1
    assert result.endpoints[0].request_body is None
    assert len(result.warnings) == 2
    assert any("HEAD" in warning for warning in result.warnings)
    assert any("multipart/form-data" in warning for warning in result.warnings)


def test_utf8_bom_and_path_extensions_are_supported(parser: OpenAPIParser) -> None:
    raw = json.dumps({"openapi": "3.0.3", "paths": {"x-comment": "metadata", "/ping": {"get": {}}}})
    result = parser.parse(b"\xef\xbb\xbf" + raw.encode("utf-8"))

    assert result.endpoints[0].path == "/ping"


@pytest.mark.parametrize("version", ["3.broken", "3.0.3-extra", "3.2.0", "2.0"])
def test_only_the_documented_versions_are_accepted(parser: OpenAPIParser, version: str) -> None:
    raw = json.dumps({"openapi": version, "paths": {"/ping": {"get": {}}}})
    with pytest.raises(InvalidOpenAPISpecError, match="версии"):
        parser.parse(raw)


def test_reserved_header_parameters_are_ignored_with_a_warning(parser: OpenAPIParser) -> None:
    raw = json.dumps(
        {
            "openapi": "3.0.3",
            "paths": {
                "/items": {
                    "get": {
                        "parameters": [
                            {"name": name, "in": "header", "schema": {"type": "string"}}
                            for name in ("Authorization", "accept", "CONTENT-TYPE", "X-Trace")
                        ]
                    }
                }
            },
        }
    )
    result = parser.parse(raw)

    assert [parameter.name for parameter in result.endpoints[0].parameters] == ["X-Trace"]
    assert len(result.warnings) == 3


def test_warnings_do_not_leak_between_documents(parser: OpenAPIParser) -> None:
    first = parser.parse(
        json.dumps({"openapi": "3.0.3", "paths": {"/ping": {"head": {}, "get": {}}}})
    )
    second = parser.parse(_spec_with_body({"type": "string"}))

    assert len(first.warnings) == 1
    assert second.warnings == ()


def test_unreadable_numeric_literal_is_a_spec_error(parser: OpenAPIParser) -> None:
    limit = sys.get_int_max_str_digits()
    if limit == 0:
        pytest.skip("Ограничение числовых литералов в интерпретаторе отключено")
    raw = _spec_with_body({"type": "integer", "minimum": "HUGE_NUMBER"})
    raw = raw.replace('"HUGE_NUMBER"', "9" * (limit + 1))

    with pytest.raises(InvalidOpenAPISpecError, match="JSON|YAML"):
        parser.parse(raw)
