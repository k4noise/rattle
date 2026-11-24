import json

import pytest

from rattle.exceptions import (
    GenerationLimitError,
    NoExampleError,
    RequestGenerationError,
    UnsupportedSchemaError,
)
from rattle.generation import RequestGenerator
from rattle.models import (
    EndpointSpec,
    ParameterSpec,
    ParamLocation,
    RequestBodySpec,
    SchemaSpec,
)
from rattle.openapi import OpenAPIParser


def test_build_base_from_parsed_openapi_keeps_typed_data_and_metadata() -> None:
    spec = OpenAPIParser().parse("""
openapi: 3.1.0
paths:
  /items/{id}:
    parameters:
      - name: id
        in: path
        schema: {type: integer, minimum: 3}
    post:
      parameters:
        - name: tags
          in: query
          explode: false
          allowReserved: true
          schema: {type: array, items: {type: string}}
        - name: X-Mode
          in: header
          schema: {type: string, enum: [fast, slow]}
        - name: session
          in: cookie
          schema: {type: boolean}
      requestBody:
        required: true
        content:
          application/problem+json:
            schema:
              type: object
              required: [name]
              properties:
                name: {type: string, minLength: 2, maxLength: 4}
                count: {type: integer, minimum: 1.5, maximum: 3}
""")
    request = RequestGenerator().build_base(spec.endpoints[0])

    assert request.method == "POST"
    assert request.path == "/items/{id}"
    assert [(p.name, p.value) for p in request.parameters] == [
        ("id", 3),
        ("tags", ["a"]),
        ("X-Mode", "fast"),
        ("session", False),
    ]
    assert [(p.style, p.explode) for p in request.parameters] == [
        ("simple", False),
        ("form", False),
        ("simple", False),
        ("form", True),
    ]
    assert request.parameters[1].allow_reserved is True
    assert request.has_body is True
    assert request.content_type == "application/problem+json"
    assert request.body == {"name": "aa", "count": 2}
    json.dumps(request.body, allow_nan=False)


@pytest.mark.parametrize(
    ("location", "style", "explode"),
    [
        ("path", "simple", False),
        ("query", "form", True),
        ("header", "simple", False),
        ("cookie", "form", True),
    ],
)
@pytest.mark.parametrize("explicit", [False, True])
def test_manual_parameter_serialization_defaults_and_explicit_flags(
    location: ParamLocation, style: str, explode: bool, explicit: bool
) -> None:
    parameter = ParameterSpec(
        "value",
        location,
        True,
        SchemaSpec(schema_type="integer"),
        style=style if explicit else None,
        explode=not explode if explicit else None,
    )
    endpoint = EndpointSpec("GET", "/{value}" if location == "path" else "/", (parameter,))
    result = RequestGenerator().build_base(endpoint).parameters[0]
    assert result.style == style
    assert result.explode is (not explode if explicit else explode)


@pytest.mark.parametrize(
    "parameter",
    [
        ParameterSpec("q", "query", False, SchemaSpec(), style="deepObject"),
        ParameterSpec("q", "query", False, SchemaSpec(), style=""),
        ParameterSpec("q", "header", False, SchemaSpec(), allow_reserved=True),
        ParameterSpec("value", "path", False, SchemaSpec()),
        ParameterSpec("Content-Type", "header", False, SchemaSpec()),
    ],
)
def test_unsupported_manual_parameter_settings_do_not_disappear(parameter: ParameterSpec) -> None:
    endpoint = EndpointSpec(
        "GET", "/{value}" if parameter.location == "path" else "/", (parameter,)
    )
    with pytest.raises(UnsupportedSchemaError, match="GET /"):
        RequestGenerator().build_base(endpoint)


def test_impossible_optional_parameters_fields_and_body_are_omitted() -> None:
    impossible = SchemaSpec(schema_type="integer", enum=("not an integer",))
    endpoint = EndpointSpec(
        "POST",
        "/",
        (ParameterSpec("q", "query", False, impossible),),
        RequestBodySpec(False, "application/json", impossible),
    )
    request = RequestGenerator().build_base(endpoint)
    assert request.parameters == ()
    assert request.has_body is False
    assert request.body is None
    assert request.content_type is None


@pytest.mark.parametrize("required_body", [False, True])
def test_json_null_body_is_not_an_absent_body(required_body: bool) -> None:
    endpoint = EndpointSpec(
        "POST",
        "/",
        request_body=RequestBodySpec(
            required_body, "application/json", SchemaSpec(schema_type="null")
        ),
    )
    request = RequestGenerator().build_base(endpoint)
    assert request.has_body is True
    assert request.body is None
    assert request.content_type == "application/json"


@pytest.mark.parametrize("target", ["parameter", "body"])
def test_impossible_required_input_is_a_generation_error(target: str) -> None:
    impossible = SchemaSpec(schema_type="integer", minimum=2, maximum=1)
    endpoint = (
        EndpointSpec("POST", "/", (ParameterSpec("q", "query", True, impossible),))
        if target == "parameter"
        else EndpointSpec(
            "POST", "/", request_body=RequestBodySpec(True, "application/json", impossible)
        )
    )
    with pytest.raises(NoExampleError, match="POST /"):
        RequestGenerator().build_base(endpoint)


@pytest.mark.parametrize("target", ["parameter", "body"])
def test_unsupported_optional_input_is_not_treated_as_an_impossible_value(target: str) -> None:
    unsupported = SchemaSpec(fmt="email")
    endpoint = (
        EndpointSpec("POST", "/", (ParameterSpec("q", "query", False, unsupported),))
        if target == "parameter"
        else EndpointSpec(
            "POST", "/", request_body=RequestBodySpec(False, "application/json", unsupported)
        )
    )
    with pytest.raises(UnsupportedSchemaError):
        RequestGenerator().build_base(endpoint)


def test_base_values_do_not_alias_model_enum_siblings_or_other_calls() -> None:
    choice = {"rows": [1]}
    schema = SchemaSpec(schema_type="object", enum=(choice,))
    endpoint = EndpointSpec(
        "POST",
        "/",
        (
            ParameterSpec("first", "query", False, schema),
            ParameterSpec("second", "query", False, schema),
        ),
        RequestBodySpec(True, "application/json", schema),
    )
    generator = RequestGenerator()
    first = generator.build_base(endpoint)
    other = generator.build_base(endpoint)
    assert isinstance(first.body, dict)
    assert isinstance(first.body["rows"], list)
    first.body["rows"].append(99)
    value = first.parameters[0].value
    assert isinstance(value, dict)
    value["rows"] = []
    assert first.parameters[1].value == choice
    assert other.body == choice
    assert other.parameters[0].value == choice
    assert choice == {"rows": [1]}


def test_generator_is_reusable_after_an_error_and_does_not_retain_its_budget() -> None:
    generator = RequestGenerator(max_nodes=10)
    bad = EndpointSpec(
        "POST",
        "/",
        request_body=RequestBodySpec(
            True, "application/json", SchemaSpec(schema_type="string", min_length=100)
        ),
    )
    generator_with_small_strings = RequestGenerator(max_string_length=1)
    with pytest.raises(GenerationLimitError):
        generator_with_small_strings.build_base(bad)
    empty = EndpointSpec("GET", "/")
    for _ in range(20):
        assert generator.build_base(empty).parameters == ()
        assert generator_with_small_strings.build_base(empty).has_body is False


@pytest.mark.parametrize(
    "endpoint",
    [
        EndpointSpec("GET", "/{id}"),
        EndpointSpec("GET", "/", (ParameterSpec("id", "path", True, SchemaSpec()),)),
        EndpointSpec(
            "GET",
            "/",
            (
                ParameterSpec("X-Mode", "header", False, SchemaSpec()),
                ParameterSpec("x-mode", "header", False, SchemaSpec()),
            ),
        ),
        EndpointSpec("POST", "/", request_body=RequestBodySpec(True, "text/plain", SchemaSpec())),
    ],
)
def test_inconsistent_manual_endpoint_models_are_explicit_errors(endpoint: EndpointSpec) -> None:
    with pytest.raises(RequestGenerationError):
        RequestGenerator().build_base(endpoint)


@pytest.mark.parametrize(
    "media_type", ["application/json", "Application/Problem+JSON; charset=utf-8"]
)
def test_json_content_type_is_preserved(media_type: str) -> None:
    endpoint = EndpointSpec(
        "POST", "/", request_body=RequestBodySpec(True, media_type, SchemaSpec())
    )
    assert RequestGenerator().build_base(endpoint).content_type == media_type


@pytest.mark.parametrize("name", ["max_depth", "max_nodes", "max_string_length"])
@pytest.mark.parametrize("bad", [True, -1, 1.5])
def test_generator_limits_are_validated(name: str, bad: int) -> None:
    with pytest.raises(ValueError, match=name):
        RequestGenerator(**{name: bad})


def test_node_budget_cannot_be_zero() -> None:
    with pytest.raises(ValueError, match="max_nodes"):
        RequestGenerator(max_nodes=0)
