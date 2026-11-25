import pytest

from rattle.exceptions import GenerationLimitError, NoExampleError, UnsupportedSchemaError
from rattle.generation import RequestGenerator
from rattle.models import EndpointSpec, ParameterSpec, ParamLocation, RequestBodySpec, SchemaSpec


def _impossible() -> SchemaSpec:
    return SchemaSpec(schema_type="integer", enum=("not an integer",))


def _body(schema: SchemaSpec, *, required: bool = True) -> EndpointSpec:
    return EndpointSpec(
        "POST", "/items", request_body=RequestBodySpec(required, "application/json", schema)
    )


def test_impossible_optional_parameter_and_body_return_explicit_warnings() -> None:
    endpoint = EndpointSpec(
        "POST",
        "/items",
        (ParameterSpec("q", "query", False, _impossible()),),
        RequestBodySpec(False, "application/json", _impossible()),
    )
    request = RequestGenerator().build_base(endpoint)
    assert request.parameters == ()
    assert request.has_body is False
    assert [(warning.kind, warning.location, warning.path) for warning in request.warnings] == [
        ("omitted_optional", "query", ("q",)),
        ("omitted_optional", "body", ()),
    ]
    assert all("enum" in warning.message for warning in request.warnings)


@pytest.mark.parametrize("location", ["query", "header", "cookie"])
def test_optional_parameter_warning_has_its_location_and_original_name(
    location: ParamLocation,
) -> None:
    endpoint = EndpointSpec("GET", "/", (ParameterSpec("X-Name", location, False, _impossible()),))
    request = RequestGenerator().build_base(endpoint)
    assert len(request.warnings) == 1
    warning = request.warnings[0]
    assert warning.location == location
    assert warning.path == ("X-Name",)
    assert warning.kind == "omitted_optional"
    assert warning.message


def test_required_impossible_values_still_raise_instead_of_becoming_warnings() -> None:
    generator = RequestGenerator()
    with pytest.raises(NoExampleError):
        generator.build_base(_body(_impossible()))
    with pytest.raises(NoExampleError):
        generator.build_base(
            EndpointSpec("GET", "/{id}", (ParameterSpec("id", "path", True, _impossible()),))
        )
    with pytest.raises(NoExampleError):
        generator.build_base(
            _body(
                SchemaSpec(
                    schema_type="object",
                    required_fields=frozenset({"id"}),
                    properties={"id": _impossible()},
                )
            )
        )


def test_nested_object_and_array_omissions_have_unambiguous_paths() -> None:
    leaf = SchemaSpec(schema_type="object", properties={"a/b~": _impossible()})
    schema = SchemaSpec(
        schema_type="object",
        properties={
            "rows": SchemaSpec(schema_type="array", items=leaf),
        },
    )
    endpoint = EndpointSpec(
        "POST",
        "/items",
        (ParameterSpec("filter", "query", False, leaf),),
        RequestBodySpec(True, "application/json", schema),
    )
    request = RequestGenerator().build_base(endpoint)
    assert request.parameters[0].value == {}
    assert request.body == {"rows": [{}]}
    assert [(warning.location, warning.path) for warning in request.warnings] == [
        ("query", ("filter", "a/b~")),
        ("body", ("rows", 0, "a/b~")),
    ]
    assert all(warning.kind == "omitted_optional" for warning in request.warnings)


def test_empty_array_fallback_reports_why_no_item_was_built() -> None:
    request = RequestGenerator().build_base(
        _body(SchemaSpec(schema_type="array", items=_impossible()))
    )
    assert request.has_body is True and request.body == []
    assert len(request.warnings) == 1
    warning = request.warnings[0]
    assert warning.kind == "empty_array"
    assert warning.location == "body" and warning.path == ()
    assert "enum" in warning.message


@pytest.mark.parametrize("kind", ["any", "object"])
def test_warnings_from_an_unselected_object_candidate_do_not_leak_into_null(kind: str) -> None:
    schema = SchemaSpec(
        schema_type=kind,
        nullable=True,
        required_fields=frozenset({"required"}),
        properties={"optional": _impossible(), "required": _impossible()},
    )
    request = RequestGenerator().build_base(_body(schema))
    assert request.body is None and request.has_body is True
    assert request.warnings == ()


def test_warnings_from_an_impossible_array_item_do_not_leak_into_the_empty_array() -> None:
    item = SchemaSpec(
        schema_type="object",
        required_fields=frozenset({"required"}),
        properties={
            "optional": _impossible(),
            "required": _impossible(),
        },
    )
    request = RequestGenerator().build_base(_body(SchemaSpec(schema_type="array", items=item)))
    assert request.body == []
    assert [(warning.kind, warning.path) for warning in request.warnings] == [("empty_array", ())]


@pytest.mark.parametrize("target", ["parameter", "body"])
def test_omitting_an_impossible_optional_root_reports_only_that_root(target: str) -> None:
    schema = SchemaSpec(
        schema_type="object",
        required_fields=frozenset({"required"}),
        properties={
            "optional": _impossible(),
            "required": _impossible(),
        },
    )
    endpoint = (
        EndpointSpec("GET", "/", (ParameterSpec("q", "query", False, schema),))
        if target == "parameter"
        else (_body(schema, required=False))
    )
    request = RequestGenerator().build_base(endpoint)
    assert len(request.warnings) == 1
    warning = request.warnings[0]
    assert warning.kind == "omitted_optional"
    assert (warning.location, warning.path) == (
        ("query", ("q",)) if target == "parameter" else ("body", ())
    )


def test_warning_order_is_stable_and_issues_do_not_repeat_for_later_roots() -> None:
    schema = SchemaSpec(schema_type="object", properties={"z": _impossible(), "a": _impossible()})
    endpoint = EndpointSpec(
        "POST",
        "/items",
        (
            ParameterSpec("filter", "query", False, schema),
            ParameterSpec("count", "query", False, SchemaSpec(schema_type="integer")),
        ),
        RequestBodySpec(True, "application/json", schema),
    )
    generator = RequestGenerator()
    expected = [("filter", "z"), ("filter", "a"), ("z",), ("a",)]
    for _ in range(3):
        assert [warning.path for warning in generator.build_base(endpoint).warnings] == expected


def test_mutations_preserve_base_diagnostics_without_warning_for_intentional_omissions() -> None:
    schema = SchemaSpec(
        schema_type="object",
        required_fields=frozenset({"id"}),
        properties={
            "id": SchemaSpec(schema_type="integer"),
            "optional": _impossible(),
        },
    )
    endpoint = _body(schema)
    generator = RequestGenerator()
    base = generator.build_base(endpoint)
    cases = list(generator.generate(endpoint))
    assert len(base.warnings) == 1
    assert base.warnings[0].path == ("optional",)
    assert any(case.kind == "missing_required" for case in cases)
    assert all(case.request.warnings == base.warnings for case in cases)
    assert generator.build_base(_body(SchemaSpec())).warnings == ()


def test_diagnostics_are_available_on_base_even_when_no_mutations_exist() -> None:
    endpoint = EndpointSpec("GET", "/", (ParameterSpec("q", "query", False, _impossible()),))
    generator = RequestGenerator()
    assert list(generator.generate(endpoint)) == []
    assert len(generator.build_base(endpoint).warnings) == 1


def test_a_suitable_enum_value_does_not_produce_omission_warnings() -> None:
    schema = SchemaSpec(schema_type="object", enum=({},), properties={"optional": _impossible()})
    request = RequestGenerator().build_base(_body(schema))
    assert request.body == {}
    assert request.warnings == ()


def test_normal_empty_arrays_and_absent_undefined_bodies_do_not_warn() -> None:
    generator = RequestGenerator()
    for endpoint in [EndpointSpec("GET", "/"), _body(SchemaSpec(schema_type="array"))]:
        assert generator.build_base(endpoint).warnings == ()


@pytest.mark.parametrize(
    ("schema", "error"),
    [
        (SchemaSpec(fmt="email"), UnsupportedSchemaError),
        (SchemaSpec(schema_type="string", min_length=3), GenerationLimitError),
    ],
)
def test_unsupported_and_over_budget_optional_values_are_still_errors(
    schema: SchemaSpec, error: type[Exception]
) -> None:
    with pytest.raises(error):
        RequestGenerator(max_string_length=1).build_base(_body(schema, required=False))
