import json

import pytest

from rattle.exceptions import NoExampleError
from rattle.generation import RequestGenerator
from rattle.models import EndpointSpec, SchemaSpec
from rattle.openapi import OpenAPIParser


def _endpoint(schema: object, *, version: str = "3.1.0") -> EndpointSpec:
    return (
        OpenAPIParser()
        .parse(
            json.dumps(
                {
                    "openapi": version,
                    "paths": {
                        "/items": {
                            "post": {
                                "requestBody": {
                                    "required": True,
                                    "content": {"application/json": {"schema": schema}},
                                }
                            }
                        }
                    },
                }
            )
        )
        .endpoints[0]
    )


def _schema(endpoint: EndpointSpec) -> SchemaSpec:
    assert endpoint.request_body is not None
    return endpoint.request_body.schema


def _union(branch: dict[str, object], *, reverse: bool = False) -> dict[str, object]:
    branches = [{"type": "null"}, branch]
    return {"anyOf": list(reversed(branches)) if reverse else branches}


@pytest.mark.parametrize("version", ["3.0.3", "3.1.0"])
@pytest.mark.parametrize("reverse", [False, True])
def test_anyof_branch_enum_and_common_enum_remain_distinguishable(
    version: str, reverse: bool
) -> None:
    union = _schema(
        _endpoint(_union({"type": "string", "enum": ["x"]}, reverse=reverse), version=version)
    )
    common = _schema(_endpoint({"type": ["string", "null"], "enum": ["x"]}, version=version))

    assert union.schema_type == common.schema_type == "string"
    assert union.nullable is common.nullable is True
    assert union.enum == ("x", None)
    assert common.enum == ("x",)
    assert union != common


@pytest.mark.parametrize("reverse", [False, True])
def test_union_null_is_not_reported_as_an_outside_enum_mutation(reverse: bool) -> None:
    endpoint = _endpoint(_union({"type": "string", "enum": ["x"]}, reverse=reverse))
    generator = RequestGenerator()
    assert generator.build_base(endpoint).body == "x"
    cases = list(generator.generate(endpoint))
    assert any(case.kind == "outside_enum" for case in cases)
    assert all(case.request.body not in ("x", None) for case in cases if case.request.has_body)


def test_common_enum_still_excludes_null_even_with_a_nullable_type() -> None:
    endpoint = _endpoint({"type": ["string", "null"], "enum": ["x"]})
    cases = list(RequestGenerator().generate(endpoint))
    outside = next(case for case in cases if case.kind == "outside_enum")
    assert outside.request.has_body is True
    assert outside.request.body is None


@pytest.mark.parametrize(
    "branch",
    [
        {"type": "string", "enum": ["x"], "minLength": 2},
        {"type": "string", "enum": ["x"], "minLength": 3, "maxLength": 1},
        {"type": "string", "enum": [1]},
        {"type": "integer", "enum": [1], "minimum": 2},
        {"type": "object", "enum": [{}], "required": ["id"]},
        {"type": "array", "enum": [["x"]], "items": {"type": "integer"}},
    ],
)
def test_impossible_non_null_branch_does_not_remove_a_separate_null_branch(
    branch: dict[str, object],
) -> None:
    endpoint = _endpoint(_union(branch))
    generator = RequestGenerator()
    request = generator.build_base(endpoint)
    assert request.has_body is True
    assert request.body is None
    cases = list(generator.generate(endpoint))
    assert all(case.request.body is not None for case in cases if case.request.has_body)


def test_impossible_nullable_type_with_a_common_enum_still_has_no_example() -> None:
    endpoint = _endpoint({"type": ["string", "null"], "enum": ["x"], "minLength": 2})
    with pytest.raises(NoExampleError):
        RequestGenerator().build_base(endpoint)


@pytest.mark.parametrize("enum", [["x", None], [None, "x"], [None]])
def test_an_explicit_null_in_the_branch_enum_is_not_added_twice(enum: list[object]) -> None:
    endpoint = _endpoint(_union({"type": "string", "enum": enum}))
    assert _schema(endpoint).enum == tuple(enum)


def test_null_union_survives_refs_allof_and_nested_anyof_without_changing_other_uses() -> None:
    spec = OpenAPIParser().parse("""
openapi: 3.1.0
components:
  schemas:
    Label: {type: string, enum: [x]}
paths:
  /items:
    post:
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: object
              required: [union, original]
              properties:
                union:
                  allOf:
                    - anyOf:
                        - type: "null"
                        - anyOf:
                            - $ref: '#/components/schemas/Label'
                            - type: "null"
                original: {$ref: '#/components/schemas/Label'}
""")
    endpoint = spec.endpoints[0]
    schema = _schema(endpoint)
    assert schema.properties["union"].enum == ("x", None)
    assert schema.properties["original"].enum == ("x",)
    cases = list(RequestGenerator().generate(endpoint))
    outside = next(
        case for case in cases if case.kind == "outside_enum" and case.path == ("union",)
    )
    assert isinstance(outside.request.body, dict)
    assert outside.request.body["union"] not in (None, "x")
    assert outside.request.body["original"] == "x"


def test_nested_nullable_enum_can_match_an_explicit_null_from_an_outer_enum() -> None:
    endpoint = _endpoint(
        {
            "type": "array",
            "enum": [[None]],
            "items": _union({"type": "string", "enum": ["x"]}),
        }
    )
    generator = RequestGenerator()
    assert generator.build_base(endpoint).body == [None]
    cases = list(generator.generate(endpoint))
    assert all(case.request.body != [None] for case in cases if case.request.has_body)


def test_boolean_enum_covering_the_union_has_no_outside_enum_case() -> None:
    endpoint = _endpoint(_union({"type": "boolean", "enum": [False, True]}))
    assert [case.kind for case in RequestGenerator().generate(endpoint)] == [
        "missing_required",
        "wrong_type",
    ]
    common = _endpoint({"type": ["boolean", "null"], "enum": [False, True]})
    outside = next(
        case for case in RequestGenerator().generate(common) if case.kind == "outside_enum"
    )
    assert outside.request.body is None and outside.request.has_body is True


def test_common_nullable_enum_inside_anyof_retains_its_existing_restrictions() -> None:
    branch = {"type": ["string", "null"], "enum": ["x"], "minLength": 2}
    request = RequestGenerator().build_base(_endpoint(_union(branch)))
    assert request.body is None and request.has_body is True
