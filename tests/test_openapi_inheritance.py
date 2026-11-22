import json

import pytest

from rattle.exceptions import InvalidOpenAPISpecError
from rattle.models import SchemaSpec
from rattle.openapi import OpenAPIParser


def _spec(
    schema: object,
    *,
    reference: bool = False,
    methods: tuple[str, ...] = ("get", "post"),
    override: object | None = None,
) -> str:
    parameter = {"name": "filter", "in": "query", "schema": schema}
    inherited = {"$ref": "#/components/parameters/Filter"} if reference else parameter
    item: dict[str, object] = {"parameters": [inherited]}
    item.update({method: {} for method in methods})
    if override is not None:
        item["get"] = {"parameters": [{"name": "filter", "in": "query", "schema": override}]}
    return json.dumps(
        {
            "openapi": "3.1.0",
            "paths": {"/items": item},
            "components": {"parameters": {"Filter": parameter}},
        }
    )


@pytest.mark.parametrize("reference", [False, True])
def test_inherited_parameter_trees_are_owned_by_each_operation(
    parser: OpenAPIParser, reference: bool
) -> None:
    result = parser.parse(
        _spec(
            {
                "type": "object",
                "enum": [{"nested": []}],
                "properties": {"child": {"type": "array", "enum": [[1]]}},
            },
            reference=reference,
        )
    )
    first, second = [endpoint.parameters[0] for endpoint in result.endpoints]
    assert first is not second
    assert first.schema is not second.schema
    assert first.schema.properties is not second.schema.properties

    first_value, second_value = first.schema.enum[0], second.schema.enum[0]
    assert isinstance(first_value, dict) and isinstance(second_value, dict)
    first_value["nested"].append("changed")
    assert second_value["nested"] == []

    first_child = first.schema.properties["child"].enum[0]
    second_child = second.schema.properties["child"].enum[0]
    assert isinstance(first_child, list) and isinstance(second_child, list)
    first_child.append(2)
    assert second_child == [1]

    first.schema.properties["extra"] = SchemaSpec(schema_type="string")
    assert "extra" not in second.schema.properties


def test_override_does_not_change_other_operations_inherited_values(parser: OpenAPIParser) -> None:
    result = parser.parse(
        _spec(
            {"enum": [{"nested": []}]},
            methods=("get", "post", "put"),
            override={"type": "string", "enum": ["own"]},
        )
    )
    own, first, second = [endpoint.parameters[0] for endpoint in result.endpoints]
    assert own.schema.enum == ("own",)
    first_value, second_value = first.schema.enum[0], second.schema.enum[0]
    assert isinstance(first_value, dict) and isinstance(second_value, dict)
    first_value["nested"].append("changed")
    assert second_value["nested"] == []
    assert own.schema.enum == ("own",)


@pytest.mark.parametrize("size", [5000, 5001])
def test_inherited_enum_visits_are_charged_for_each_operation(
    parser: OpenAPIParser, size: int
) -> None:
    raw = _spec({"enum": [None] * size})
    if size == 5001:
        with pytest.raises(InvalidOpenAPISpecError, match="лимит.*enum"):
            parser.parse(raw)
    else:
        result = parser.parse(raw)
        assert len(result.endpoints) == 2
        assert all(len(endpoint.parameters[0].schema.enum) == size for endpoint in result.endpoints)

    recovered = parser.parse(_spec({"enum": ["ok"]}))
    assert all(endpoint.parameters[0].schema.enum == ("ok",) for endpoint in recovered.endpoints)


def test_inherited_schema_visits_also_use_the_document_budget(parser: OpenAPIParser) -> None:
    schema = {"properties": {f"field{index}": {"type": "string"} for index in range(5000)}}
    with pytest.raises(InvalidOpenAPISpecError, match="лимит.*схем"):
        parser.parse(_spec(schema))


@pytest.mark.parametrize("reference", [False, True])
def test_inherited_header_warnings_identify_the_method_and_path(
    parser: OpenAPIParser, reference: bool
) -> None:
    header = {"name": "Authorization", "in": "header"}
    parameter = {"$ref": "#/components/parameters/Auth"} if reference else header
    raw = json.dumps(
        {
            "openapi": "3.1.0",
            "paths": {
                "/items": {"parameters": [parameter], "get": {}, "post": {}},
                "/other": {"parameters": [parameter], "get": {}},
            },
            "components": {"parameters": {"Auth": header}},
        }
    )
    result = parser.parse(raw)
    message = "пропущен зарезервированный header-параметр Authorization"

    assert result.warnings == tuple(
        f"{method} {path} (уровень пути): {message}"
        for method, path in (("GET", "/items"), ("POST", "/items"), ("GET", "/other"))
    )
    assert all(endpoint.parameters == () for endpoint in result.endpoints)


def test_header_warnings_distinguish_path_and_operation_declarations(parser: OpenAPIParser) -> None:
    header = {"name": "Authorization", "in": "header"}
    raw = json.dumps(
        {
            "openapi": "3.1.0",
            "paths": {
                "/items": {
                    "parameters": [header],
                    "get": {"parameters": [header]},
                    "post": {},
                }
            },
        }
    )
    result = parser.parse(raw)
    message = "пропущен зарезервированный header-параметр Authorization"

    assert result.warnings == (
        f"GET /items (уровень пути): {message}",
        f"GET /items (уровень операции): {message}",
        f"POST /items (уровень пути): {message}",
    )
