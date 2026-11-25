import json

import pytest

from rattle._schema_values import SchemaValues, ValueLimits
from rattle.exceptions import GenerationLimitError, NoExampleError, UnsupportedSchemaError
from rattle.models import SchemaSpec


@pytest.mark.parametrize(
    ("schema", "expected"),
    [
        (SchemaSpec(schema_type="string", min_length=2, max_length=3), "aa"),
        (SchemaSpec(schema_type="integer", minimum=0.2, maximum=2.8), 1),
        (SchemaSpec(schema_type="integer", minimum=2**100 + 1), 2**100 + 1),
        (SchemaSpec(schema_type="number", minimum=0.25, maximum=0.75), 0.25),
        (SchemaSpec(schema_type="number", maximum=-3), -3),
        (SchemaSpec(schema_type="boolean"), False),
        (SchemaSpec(schema_type="null"), None),
        (SchemaSpec(), None),
        (SchemaSpec(schema_type="array", items=SchemaSpec(schema_type="string")), ["a"]),
        (
            SchemaSpec(schema_type="object", required_fields=frozenset({"missing"})),
            {"missing": None},
        ),
        (SchemaSpec(schema_type="boolean", enum=(1, True)), True),
        (SchemaSpec(schema_type="integer", enum=(True, 1.0)), 1.0),
        (SchemaSpec(schema_type="string", minimum=10, maximum=1), "a"),
        (SchemaSpec(schema_type="integer", min_length=5, max_length=1), 0),
        (SchemaSpec(schema_type="integer", minimum=2, maximum=1, nullable=True), None),
        (SchemaSpec(minimum=2, maximum=1, min_length=5, max_length=1), None),
        (SchemaSpec(schema_type="array", items=SchemaSpec(schema_type="integer", enum=("x",))), []),
    ],
)
def test_base_values_satisfy_applicable_constraints(schema: SchemaSpec, expected: object) -> None:
    values = SchemaValues(ValueLimits())
    actual = values.build(schema)
    assert type(actual) is type(expected)
    assert json.dumps(actual, sort_keys=True) == json.dumps(expected, sort_keys=True)
    assert values.matches(schema, actual)


@pytest.mark.parametrize(
    "schema",
    [
        SchemaSpec(schema_type="integer", minimum=1, maximum=0),
        SchemaSpec(schema_type="integer", minimum=0.1, maximum=0.9),
        SchemaSpec(schema_type="string", min_length=3, max_length=2),
        SchemaSpec(schema_type="boolean", enum=(0, 1)),
        SchemaSpec(schema_type="integer", enum=(True, "abc")),
        SchemaSpec(schema_type="string", nullable=True, enum=(1,)),
    ],
)
def test_unsatisfiable_supported_choices_are_explicit_errors(schema: SchemaSpec) -> None:
    with pytest.raises(NoExampleError):
        SchemaValues(ValueLimits()).build(schema)


def test_enum_is_filtered_by_nested_constraints_and_copied() -> None:
    first = {"flag": 1}
    second = {"flag": True}
    schema = SchemaSpec(
        schema_type="object",
        enum=(first, second),
        properties={"flag": SchemaSpec(schema_type="boolean")},
    )
    result = SchemaValues(ValueLimits()).build(schema)
    assert isinstance(result, dict)
    assert result["flag"] is True
    result["flag"] = False
    assert second["flag"] is True


def test_optional_impossible_properties_can_be_absent_but_required_ones_cannot() -> None:
    child = SchemaSpec(schema_type="integer", enum=("abc",))
    schema = SchemaSpec(schema_type="object", properties={"optional": child})
    assert SchemaValues(ValueLimits()).build(schema) == {}
    schema.properties["required"] = child
    required = SchemaSpec(
        schema_type="object", properties=schema.properties, required_fields=frozenset({"required"})
    )
    with pytest.raises(NoExampleError):
        SchemaValues(ValueLimits()).build(required)


@pytest.mark.parametrize(
    "schema",
    [SchemaSpec(pattern=".*"), SchemaSpec(fmt="email"), SchemaSpec(is_recursive=True)],
)
def test_unimplemented_features_are_not_silently_ignored(schema: SchemaSpec) -> None:
    with pytest.raises(UnsupportedSchemaError):
        SchemaValues(ValueLimits()).build(schema)


def test_manual_schema_cycles_and_non_json_enum_are_rejected() -> None:
    schema = SchemaSpec(schema_type="object")
    schema.properties["self"] = schema
    with pytest.raises(UnsupportedSchemaError, match="Рекурс"):
        SchemaValues(ValueLimits()).build(schema)

    loop: list[object] = []
    loop.append(loop)
    for value in (loop, {1: "bad key"}, float("nan"), b"bytes"):
        with pytest.raises(UnsupportedSchemaError):
            SchemaValues(ValueLimits()).build(SchemaSpec(enum=(value,)))


@pytest.mark.parametrize(
    ("limits", "schema"),
    [
        (ValueLimits(max_nodes=1), SchemaSpec()),
        (ValueLimits(max_string_length=2), SchemaSpec(schema_type="string", min_length=3)),
        (ValueLimits(max_depth=0), SchemaSpec(schema_type="array", items=SchemaSpec())),
        (
            ValueLimits(max_nodes=20),
            SchemaSpec(
                schema_type="object", required_fields=frozenset(f"field{i}" for i in range(100))
            ),
        ),
        (
            ValueLimits(max_depth=0),
            SchemaSpec(schema_type="object", required_fields=frozenset({"x"})),
        ),
    ],
)
def test_generation_resources_are_bounded(limits: ValueLimits, schema: SchemaSpec) -> None:
    with pytest.raises(GenerationLimitError):
        SchemaValues(limits).build(schema)


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [(True, 1, False), (False, 0.0, False), (1, 1.0, True), ({"x": [True]}, {"x": [1]}, False)],
)
def test_enum_equality_distinguishes_booleans_from_numbers(
    left: object, right: object, expected: bool
) -> None:
    values = SchemaValues(ValueLimits())
    assert values.equal(values.clone(left), values.clone(right)) is expected
