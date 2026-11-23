import json

import pytest

from rattle.exceptions import InvalidOpenAPISpecError
from rattle.models import ParameterSpec, ParamLocation
from rattle.openapi import OpenAPIParser


def _parse_parameter(
    parser: OpenAPIParser, location: ParamLocation, settings: dict[str, object] | None = None
) -> ParameterSpec:
    path = "/items/{value}" if location == "path" else "/items"
    parameter: dict[str, object] = {"name": "value", "in": location, "schema": {"type": "string"}}
    parameter.update(settings or {})
    raw = json.dumps({"openapi": "3.1.0", "paths": {path: {"get": {"parameters": [parameter]}}}})
    return parser.parse(raw).endpoints[0].parameters[0]


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize(
    ("location", "style", "explode"),
    [
        ("query", "form", True),
        ("cookie", "form", True),
        ("path", "simple", False),
        ("header", "simple", False),
    ],
)
def test_parameter_serialization_defaults_are_normalized(
    parser: OpenAPIParser, location: ParamLocation, style: str, explode: bool, explicit: bool
) -> None:
    settings: dict[str, object] = {"style": style, "explode": explode} if explicit else {}
    parameter = _parse_parameter(parser, location, settings)

    assert parameter.style == style
    assert parameter.explode is explode
    assert parameter.allow_reserved is False


@pytest.mark.parametrize("location", ["path", "query", "header", "cookie"])
@pytest.mark.parametrize("explode", [False, True])
def test_supported_explode_settings_are_not_lost(
    parser: OpenAPIParser, location: ParamLocation, explode: bool
) -> None:
    assert _parse_parameter(parser, location, {"explode": explode}).explode is explode


def test_allow_reserved_is_preserved_for_query_parameters(parser: OpenAPIParser) -> None:
    parameter = _parse_parameter(parser, "query", {"allowReserved": True})
    assert parameter.allow_reserved is True


@pytest.mark.parametrize("location", ["path", "header", "cookie"])
def test_allow_reserved_is_not_silently_ignored_outside_query(
    parser: OpenAPIParser, location: ParamLocation
) -> None:
    with pytest.raises(InvalidOpenAPISpecError, match="allowReserved"):
        _parse_parameter(parser, location, {"allowReserved": True})


@pytest.mark.parametrize(
    ("location", "style"),
    [
        ("query", "deepObject"),
        ("query", "spaceDelimited"),
        ("query", "pipeDelimited"),
        ("path", "matrix"),
        ("path", "label"),
        ("header", "form"),
        ("cookie", "simple"),
        ("query", "unknown"),
        ("query", ""),
    ],
)
def test_unsupported_styles_are_explicit_errors(
    parser: OpenAPIParser, location: ParamLocation, style: str
) -> None:
    with pytest.raises(InvalidOpenAPISpecError, match="style"):
        _parse_parameter(parser, location, {"style": style})


@pytest.mark.parametrize(
    "settings",
    [
        {"style": True},
        {"explode": "false"},
        {"explode": None},
        {"allowReserved": "true"},
        {"allowEmptyValue": True},
        {"allowEmptyValue": "false"},
    ],
)
def test_bad_or_unsupported_serialization_flags_are_rejected(
    parser: OpenAPIParser, settings: dict[str, object]
) -> None:
    with pytest.raises(InvalidOpenAPISpecError):
        _parse_parameter(parser, "query", settings)


def test_explicit_false_flags_keep_default_behavior(parser: OpenAPIParser) -> None:
    parameter = _parse_parameter(
        parser, "header", {"allowReserved": False, "allowEmptyValue": False}
    )
    assert parameter.allow_reserved is False
    assert parameter.style == "simple"


def test_operation_override_uses_its_own_serialization_defaults(parser: OpenAPIParser) -> None:
    inherited = {"name": "q", "in": "query", "explode": False, "allowReserved": True}
    override = {"name": "q", "in": "query"}
    raw = json.dumps(
        {
            "openapi": "3.1.0",
            "paths": {"/items": {"parameters": [inherited], "get": {"parameters": [override]}}},
        }
    )
    parameter = parser.parse(raw).endpoints[0].parameters[0]
    assert parameter.explode is True
    assert parameter.allow_reserved is False
