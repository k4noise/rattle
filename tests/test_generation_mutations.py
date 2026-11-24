import json
from collections.abc import Iterator
from typing import cast

import pytest

from rattle.exceptions import GenerationLimitError, UnsupportedSchemaError
from rattle.generation import RequestGenerator
from rattle.models import EndpointSpec, ParameterSpec, RequestBodySpec, SchemaSpec
from rattle.requests import JSONValue, MutationCase


def _body(schema: SchemaSpec, *, required: bool = False) -> EndpointSpec:
    return EndpointSpec(
        "POST", "/", request_body=RequestBodySpec(required, "application/json", schema)
    )


def test_initial_mutation_kinds_and_metadata() -> None:
    endpoint = _body(
        SchemaSpec(schema_type="integer", enum=(2, 3), minimum=2, maximum=4), required=True
    )
    generator = RequestGenerator()
    base = generator.build_base(endpoint)
    cases = list(generator.generate(endpoint))

    assert [case.kind for case in cases] == [
        "missing_required",
        "wrong_type",
        "outside_enum",
        "below_minimum",
        "above_maximum",
    ]
    assert all(case.location == "body" and case.path == () for case in cases)
    assert cases[0].request.has_body is False
    assert cases[0].request.content_type is None
    assert cases[0].request.body is None
    assert isinstance(cases[1].request.body, str)
    assert cases[2].request.body not in (2, 3)
    assert isinstance(cases[3].request.body, int) and cases[3].request.body < 2
    assert isinstance(cases[4].request.body, int) and cases[4].request.body > 4
    assert base.body == 2 and base.has_body is True


@pytest.mark.parametrize(
    ("schema", "kind", "expected"),
    [
        (SchemaSpec(schema_type="string", min_length=2), "too_short", "a"),
        (SchemaSpec(schema_type="string", max_length=3), "too_long", "aaaa"),
        (SchemaSpec(schema_type="string", max_length=0), "too_long", "a"),
        (SchemaSpec(schema_type="integer", minimum=0.2), "below_minimum", 0),
        (SchemaSpec(schema_type="integer", maximum=0.8), "above_maximum", 1),
        (SchemaSpec(schema_type="number", minimum=-2.5), "below_minimum", -3),
        (SchemaSpec(schema_type="number", maximum=-2.5), "above_maximum", -2),
        (SchemaSpec(schema_type="number", maximum=1e20), "above_maximum", 10**20 + 1),
        (SchemaSpec(schema_type="integer", minimum=2**100), "below_minimum", 2**100 - 1),
    ],
)
def test_boundary_mutations_really_cross_the_boundary(
    schema: SchemaSpec, kind: str, expected: object
) -> None:
    case = next(case for case in RequestGenerator().generate(_body(schema)) if case.kind == kind)
    assert case.request.body == expected
    json.dumps(case.request.body, allow_nan=False)


@pytest.mark.parametrize("bound", [-1e308, 1e308])
def test_number_boundaries_do_not_round_back_or_become_infinite(bound: float) -> None:
    endpoint = _body(SchemaSpec(schema_type="number", minimum=bound, maximum=bound))
    cases = {case.kind: case for case in RequestGenerator().generate(endpoint)}
    below, above = cases["below_minimum"].request.body, cases["above_maximum"].request.body
    assert isinstance(below, int) and below < bound
    assert isinstance(above, int) and above > bound
    json.dumps([below, above], allow_nan=False)


@pytest.mark.parametrize(
    ("schema", "expected"),
    [
        (SchemaSpec(schema_type="integer", min_length=2, max_length=1), ["wrong_type"]),
        (SchemaSpec(schema_type="string", minimum=2, maximum=1), ["wrong_type"]),
        (SchemaSpec(schema_type="string", min_length=0), ["wrong_type"]),
        (SchemaSpec(schema_type="null", min_length=2, maximum=0), ["wrong_type"]),
        (SchemaSpec(), []),
        (SchemaSpec(minimum=1), ["below_minimum"]),
        (SchemaSpec(max_length=1), ["too_long"]),
    ],
)
def test_inapplicable_and_impossible_mutations_are_not_reported(
    schema: SchemaSpec, expected: list[str]
) -> None:
    assert [case.kind for case in RequestGenerator().generate(_body(schema))] == expected


@pytest.mark.parametrize(
    "kind", ["string", "integer", "number", "boolean", "object", "array", "null"]
)
@pytest.mark.parametrize("nullable", [False, True])
def test_wrong_type_is_not_an_allowed_nullable_value(kind: str, nullable: bool) -> None:
    endpoint = _body(SchemaSpec(schema_type=kind, nullable=nullable))
    case = next(case for case in RequestGenerator().generate(endpoint) if case.kind == "wrong_type")
    assert case.request.body is not None
    if kind == "string":
        assert not isinstance(case.request.body, str)
    elif kind in {"integer", "number"}:
        assert isinstance(case.request.body, (str, bool, list, dict))
    elif kind == "boolean":
        assert not isinstance(case.request.body, bool)
    elif kind == "object":
        assert not isinstance(case.request.body, dict)
    elif kind == "array":
        assert not isinstance(case.request.body, list)


@pytest.mark.parametrize(
    ("schema", "expected"),
    [
        (SchemaSpec(schema_type="boolean", enum=(1, True)), False),
        (SchemaSpec(schema_type="integer", enum=(False, 0)), 1),
        (SchemaSpec(schema_type="number", enum=(0.0, 1.0)), 2),
        (SchemaSpec(schema_type="string", enum=("", "0")), "1"),
        (SchemaSpec(schema_type="object", enum=({}, {"": 0})), {"": 1}),
        (SchemaSpec(schema_type="array", enum=([], [False])), [0]),
        (SchemaSpec(schema_type="boolean", nullable=True, enum=(False, True)), None),
        (SchemaSpec(enum=(None, False, True, 0, 1)), 2),
    ],
)
def test_outside_enum_has_an_allowed_type_and_uses_json_equality(
    schema: SchemaSpec, expected: object
) -> None:
    case = next(
        case for case in RequestGenerator().generate(_body(schema)) if case.kind == "outside_enum"
    )
    assert type(case.request.body) is type(expected)
    assert case.request.body == expected


@pytest.mark.parametrize(
    "schema",
    [
        SchemaSpec(schema_type="boolean", enum=(False, True)),
        SchemaSpec(schema_type="boolean", nullable=True, enum=(None, False, True)),
        SchemaSpec(schema_type="null", enum=(None,)),
    ],
)
def test_enum_covering_the_entire_type_has_no_outside_enum_mutation(schema: SchemaSpec) -> None:
    assert [case.kind for case in RequestGenerator().generate(_body(schema))] == ["wrong_type"]


def test_required_fields_are_sorted_and_nested_fields_keep_declaration_order() -> None:
    schema = SchemaSpec(
        schema_type="object",
        required_fields=frozenset({"z", "a", "undeclared"}),
        properties={"z": SchemaSpec(schema_type="integer"), "a": SchemaSpec(schema_type="string")},
    )
    cases = list(RequestGenerator().generate(_body(schema)))
    assert [(case.kind, case.path) for case in cases] == [
        ("missing_required", ("a",)),
        ("missing_required", ("undeclared",)),
        ("missing_required", ("z",)),
        ("wrong_type", ()),
        ("wrong_type", ("z",)),
        ("wrong_type", ("a",)),
    ]
    assert cases[0].request.body == {"z": 0, "undeclared": None}
    assert cases[-1].request.body == {"z": 0, "a": 0, "undeclared": None}


def test_nested_array_paths_and_single_local_edits() -> None:
    schema = SchemaSpec(
        schema_type="object",
        properties={
            "a/b~": SchemaSpec(
                schema_type="array",
                enum=([{"id": 2}, {"id": 3}],),
                items=SchemaSpec(
                    schema_type="object",
                    required_fields=frozenset({"id"}),
                    properties={"id": SchemaSpec(schema_type="integer", minimum=1)},
                ),
            )
        },
    )
    cases = list(RequestGenerator().generate(_body(schema)))
    omitted = next(
        case for case in cases if case.path == ("a/b~", 0, "id") and case.kind == "missing_required"
    )
    assert omitted.request.body == {"a/b~": [{}, {"id": 3}]}
    below = next(
        case for case in cases if case.path == ("a/b~", 1, "id") and case.kind == "below_minimum"
    )
    assert below.request.body == {"a/b~": [{"id": 2}, {"id": 0}]}


def test_parameters_precede_body_and_keep_their_own_location() -> None:
    schema = SchemaSpec(schema_type="integer")
    endpoint = EndpointSpec(
        "POST",
        "/{id}",
        (
            ParameterSpec("q", "query", True, schema),
            ParameterSpec("id", "path", True, schema),
            ParameterSpec("X-Id", "header", True, schema),
            ParameterSpec("sid", "cookie", True, schema),
        ),
        RequestBodySpec(True, "application/json", schema),
    )
    generator = RequestGenerator()
    cases = list(generator.generate(endpoint))
    assert [(case.location, case.path, case.kind) for case in cases] == [
        (location, (name,), kind)
        for location, name in [
            ("query", "q"),
            ("path", "id"),
            ("header", "X-Id"),
            ("cookie", "sid"),
        ]
        for kind in ["missing_required", "wrong_type"]
    ] + [("body", (), "missing_required"), ("body", (), "wrong_type")]
    base = generator.build_base(endpoint)
    for index in range(4):
        omitted = cases[index * 2].request
        assert omitted.path == "/{id}"
        assert omitted.parameters == base.parameters[:index] + base.parameters[index + 1 :]
        assert omitted.has_body is True
        assert omitted.body == base.body
        changed = cases[index * 2 + 1].request
        assert changed.parameters[index].style == base.parameters[index].style
        assert changed.parameters[index].explode == base.parameters[index].explode
        assert changed.parameters[:index] == base.parameters[:index]
        assert changed.parameters[index + 1 :] == base.parameters[index + 1 :]


def test_optional_parameter_cannot_be_omitted_but_its_required_child_can() -> None:
    endpoint = EndpointSpec(
        "GET",
        "/",
        (
            ParameterSpec(
                "filter",
                "query",
                False,
                SchemaSpec(
                    schema_type="object",
                    required_fields=frozenset({"name"}),
                    properties={"name": SchemaSpec(schema_type="string")},
                ),
            ),
        ),
    )
    cases = list(RequestGenerator().generate(endpoint))
    assert [(case.kind, case.path) for case in cases] == [
        ("missing_required", ("filter", "name")),
        ("wrong_type", ("filter",)),
        ("wrong_type", ("filter", "name")),
    ]
    assert cases[0].request.parameters[0].value == {}


def _corrupt(value: JSONValue) -> None:
    if isinstance(value, list):
        for child in value:
            _corrupt(child)
        value.append("consumer mutation")
    elif isinstance(value, dict):
        for child in value.values():
            _corrupt(child)
        value["consumer mutation"] = True


def test_consumer_mutations_do_not_affect_other_cases_base_or_parser_models() -> None:
    enum_value = {"rows": [{"id": 2}]}
    shared = SchemaSpec(
        schema_type="object",
        enum=(enum_value,),
        properties={
            "rows": SchemaSpec(
                schema_type="array",
                items=SchemaSpec(
                    schema_type="object",
                    required_fields=frozenset({"id"}),
                    properties={"id": SchemaSpec(schema_type="integer", minimum=1)},
                ),
            ),
        },
    )
    endpoint = EndpointSpec(
        "POST",
        "/",
        (
            ParameterSpec("first", "query", True, shared),
            ParameterSpec("second", "cookie", False, shared),
        ),
        RequestBodySpec(True, "application/json", shared),
    )
    generator = RequestGenerator()
    base = generator.build_base(endpoint)
    reference = list(generator.generate(endpoint))
    actual = generator.generate(endpoint)
    for expected in reference:
        case = next(actual)
        assert case == expected
        _corrupt(case.request.body)
        for parameter in case.request.parameters:
            _corrupt(parameter.value)
    with pytest.raises(StopIteration):
        next(actual)
    assert base.body == {"rows": [{"id": 2}]}
    assert base.parameters[0].value == base.body
    assert base.parameters[1].value == base.body
    assert enum_value == {"rows": [{"id": 2}]}
    assert generator.build_base(endpoint) == base


def test_generation_is_lazy_and_zero_limit_does_not_inspect_the_schema() -> None:
    endpoint = _body(SchemaSpec(pattern=".*"))
    iterator = RequestGenerator().generate(endpoint)
    assert isinstance(iterator, Iterator)
    assert iter(iterator) is iterator
    with pytest.raises(UnsupportedSchemaError, match="POST /"):
        next(iterator)
    assert list(RequestGenerator().generate(endpoint, limit=0)) == []


def test_limit_does_not_compute_the_next_variant_or_exhaust_its_resources() -> None:
    endpoint = _body(SchemaSpec(schema_type="string", max_length=4))
    generator = RequestGenerator(max_string_length=4)
    cases = list(generator.generate(endpoint, limit=1))
    assert len(cases) == 1 and cases[0].kind == "wrong_type"
    iterator = generator.generate(endpoint, limit=2)
    assert next(iterator).kind == "wrong_type"
    with pytest.raises(GenerationLimitError, match="POST /"):
        next(iterator)
    assert len(list(generator.generate(endpoint, limit=1))) == 1


@pytest.mark.parametrize("limit", [0, 1, 2, 3, 10, 100])
def test_limited_generation_is_a_reproducible_prefix(limit: int) -> None:
    endpoint = _body(
        SchemaSpec(schema_type="integer", minimum=2, maximum=4, enum=(2, 3)), required=True
    )
    generator = RequestGenerator()
    expected = list(generator.generate(endpoint))
    assert list(generator.generate(endpoint, limit=limit)) == expected[:limit]
    assert list(generator.generate(endpoint, limit=limit)) == expected[:limit]


@pytest.mark.parametrize("limit", [True, -1, 1.5, None, "2"])
def test_invalid_variant_limit_is_rejected_immediately(limit: object) -> None:
    with pytest.raises(ValueError, match="limit"):
        RequestGenerator().generate(EndpointSpec("GET", "/"), limit=cast(int, limit))


def test_iterators_on_one_generator_have_independent_state() -> None:
    endpoint = _body(SchemaSpec(schema_type="string", min_length=2, max_length=4))
    generator = RequestGenerator()
    first, second = generator.generate(endpoint), generator.generate(endpoint)
    assert next(first) == next(second)
    assert next(first).kind == "too_short"
    assert next(first).kind == "too_long"
    assert next(second).kind == "too_short"
    assert next(second).kind == "too_long"
    assert list(first) == list(second) == []


def test_absent_optional_inputs_and_empty_endpoints_have_no_mutations() -> None:
    impossible = SchemaSpec(schema_type="integer", enum=("abc",))
    endpoint = EndpointSpec(
        "POST",
        "/",
        (ParameterSpec("q", "query", False, impossible),),
        RequestBodySpec(False, "application/json", impossible),
    )
    assert list(RequestGenerator().generate(endpoint)) == []
    assert list(RequestGenerator().generate(EndpointSpec("GET", "/"))) == []


def test_required_null_body_can_be_omitted_or_replaced_with_another_type() -> None:
    cases = list(RequestGenerator().generate(_body(SchemaSpec(schema_type="null"), required=True)))
    assert [case.kind for case in cases] == ["missing_required", "wrong_type"]
    assert cases[0].request.has_body is False
    assert cases[1].request.has_body is True
    assert cases[1].request.body is not None


@pytest.mark.parametrize("container", ["object", "array"])
def test_replacement_depth_is_counted_from_its_position_in_the_request(container: str) -> None:
    child = SchemaSpec(schema_type="object", enum=({},))
    schema = (
        SchemaSpec(schema_type="object", properties={"child": child})
        if container == "object"
        else (SchemaSpec(schema_type="array", items=child))
    )
    generator = RequestGenerator(max_depth=1)
    iterator = generator.generate(_body(schema))
    assert next(iterator).kind == "wrong_type"
    assert next(iterator).kind == "wrong_type"
    # {"": 0} само по себе имеет глубину 1, но на месте child — уже 2.
    with pytest.raises(GenerationLimitError, match="глубина"):
        next(iterator)


@pytest.mark.parametrize(
    "kind", ["string", "integer", "number", "boolean", "null", "object", "array"]
)
def test_zero_string_budget_still_allows_empty_strings_and_other_json_types(kind: str) -> None:
    generator = RequestGenerator(max_string_length=0)
    endpoint = _body(SchemaSpec(schema_type=kind))
    base = generator.build_base(endpoint)
    if kind == "string":
        assert base.body == ""
    cases = list(generator.generate(endpoint))
    assert [case.kind for case in cases] == ["wrong_type"]
    if isinstance(cases[0].request.body, str):
        assert cases[0].request.body == ""


def test_budget_covers_comparisons_and_copies_across_the_whole_iteration() -> None:
    generator = RequestGenerator(max_nodes=100)
    endpoint = _body(SchemaSpec(schema_type="integer", enum=tuple(range(20))))
    iterator = generator.generate(endpoint)
    assert next(iterator).kind == "wrong_type"
    with pytest.raises(GenerationLimitError, match="бюджет"):
        next(iterator)
    assert list(generator.generate(EndpointSpec("GET", "/"))) == []


def test_default_limit_caps_a_large_schema_without_building_a_list_of_variants() -> None:
    endpoint = _body(
        SchemaSpec(
            schema_type="object",
            properties={str(index): SchemaSpec(schema_type="integer") for index in range(150)},
        )
    )
    cases: list[MutationCase] = list(RequestGenerator().generate(endpoint))
    assert len(cases) == 100
    assert cases[-1].path == ("98",)
