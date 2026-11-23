"""Ограниченное построение и проверка JSON-значений по моделям парсера."""

import math
from dataclasses import dataclass
from typing import TypeGuard

from rattle.exceptions import GenerationLimitError, NoExampleError, UnsupportedSchemaError
from rattle.models import SchemaSpec
from rattle.requests import JSONValue

_TYPES = frozenset({"any", "string", "integer", "number", "boolean", "null", "object", "array"})


def is_number(value: object) -> TypeGuard[int | float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


@dataclass(frozen=True)
class ValueLimits:
    max_depth: int = 32
    max_nodes: int = 100_000
    max_string_length: int = 4096

    def __post_init__(self) -> None:
        for name, value in (
            ("max_depth", self.max_depth),
            ("max_nodes", self.max_nodes),
            ("max_string_length", self.max_string_length),
        ):
            minimum = 1 if name == "max_nodes" else 0
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise ValueError(f"Недопустимый лимит {name}")


class SchemaValues:
    """Контекст одного вызова генератора: бюджеты и снимки enum не разделяются."""

    def __init__(self, limits: ValueLimits) -> None:
        self.limits = limits
        self._visits = 0
        self._prepared: dict[int, SchemaSpec] = {}
        self._enums: dict[int, tuple[JSONValue, ...]] = {}

    def step(self, depth: int = 0) -> None:
        if depth > self.limits.max_depth:
            raise GenerationLimitError("Превышена глубина генерации")
        self._visits += 1
        if self._visits > self.limits.max_nodes:
            raise GenerationLimitError("Превышен бюджет посещений при генерации")

    def check_string(self, value: str) -> None:
        if len(value) > self.limits.max_string_length:
            raise GenerationLimitError("Превышена максимальная длина строки")

    def text(self, length: int) -> str:
        self.step()
        if length > self.limits.max_string_length:
            raise GenerationLimitError("Превышена максимальная длина строки")
        return "a" * length

    def clone(
        self, value: object, depth: int = 0, active: frozenset[int] = frozenset()
    ) -> JSONValue:
        self.step(depth)
        if value is None or isinstance(value, (bool, int)):
            return value
        if isinstance(value, str):
            self.check_string(value)
            return value
        if isinstance(value, float) and math.isfinite(value):
            return value
        if isinstance(value, (list, dict)):
            if id(value) in active:
                raise UnsupportedSchemaError("Цикл внутри JSON-значения")
            active = active | {id(value)}
            if isinstance(value, list):
                return [self.clone(item, depth + 1, active) for item in value]
            result: dict[str, JSONValue] = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise UnsupportedSchemaError("Ключ JSON-объекта должен быть строкой")
                self.check_string(key)
                result[key] = self.clone(item, depth + 1, active)
            return result
        raise UnsupportedSchemaError("Допускаются только конечные JSON-значения")

    def equal(self, left: JSONValue, right: JSONValue, depth: int = 0) -> bool:
        """JSON-сравнение для enum: true != 1, но числа 1 и 1.0 равны."""
        self.step(depth)
        if is_number(left) and is_number(right):
            return left == right
        if type(left) is not type(right):
            return False
        if isinstance(left, list) and isinstance(right, list):
            return len(left) == len(right) and all(
                self.equal(a, b, depth + 1) for a, b in zip(left, right, strict=True)
            )
        if isinstance(left, dict) and isinstance(right, dict):
            return left.keys() == right.keys() and all(
                self.equal(item, right[key], depth + 1) for key, item in left.items()
            )
        return left == right

    def prepare(
        self, schema: SchemaSpec, active: frozenset[int] = frozenset(), depth: int = 0
    ) -> None:
        self.step(depth)
        identity = id(schema)
        if identity in active or schema.is_recursive:
            raise UnsupportedSchemaError("Рекурсивная генерация пока не поддерживается")
        if identity in self._prepared:
            return
        if schema.schema_type not in _TYPES:
            raise UnsupportedSchemaError(f"Неизвестный тип: {schema.schema_type}")
        if schema.pattern is not None or schema.fmt is not None:
            raise UnsupportedSchemaError("Генерация по pattern/format пока не поддерживается")
        for name, bound in (("minimum", schema.minimum), ("maximum", schema.maximum)):
            if bound is not None and (
                not is_number(bound) or isinstance(bound, float) and not math.isfinite(bound)
            ):
                raise UnsupportedSchemaError(f"Некорректное ограничение {name}")
        for name, length in (("minLength", schema.min_length), ("maxLength", schema.max_length)):
            if length is not None and (
                isinstance(length, bool) or not isinstance(length, int) or length < 0
            ):
                raise UnsupportedSchemaError(f"Некорректное ограничение {name}")
        self._enums[identity] = tuple(self.clone(value) for value in schema.enum)
        active = active | {identity}
        for name, child in schema.properties.items():
            if not isinstance(name, str) or not isinstance(child, SchemaSpec):
                raise UnsupportedSchemaError("Некорректное описание properties")
            self.prepare(child, active, depth + 1)
        if any(not isinstance(name, str) for name in schema.required_fields):
            raise UnsupportedSchemaError("required должен содержать имена полей")
        if schema.items is not None:
            self.prepare(schema.items, active, depth + 1)
        self._prepared[identity] = schema

    def allows_type(self, schema: SchemaSpec, value: JSONValue) -> bool:
        if value is None:
            return schema.nullable or schema.schema_type in {"any", "null"}
        kind = schema.schema_type
        if kind == "any":
            return True
        if kind == "integer":
            return is_number(value) and (isinstance(value, int) or value.is_integer())
        if kind == "number":
            return is_number(value)
        if kind == "string":
            return isinstance(value, str)
        if kind == "boolean":
            return isinstance(value, bool)
        if kind == "object":
            return isinstance(value, dict)
        return kind == "array" and isinstance(value, list)

    def matches(self, schema: SchemaSpec, value: JSONValue, depth: int = 0) -> bool:
        self.step(depth)
        choices = self._enums[id(schema)]
        if choices and not any(self.equal(value, choice) for choice in choices):
            return False
        if not self.allows_type(schema, value):
            return False
        if is_number(value):
            if schema.minimum is not None and value < schema.minimum:
                return False
            if schema.maximum is not None and value > schema.maximum:
                return False
        if isinstance(value, str):
            if schema.min_length is not None and len(value) < schema.min_length:
                return False
            if schema.max_length is not None and len(value) > schema.max_length:
                return False
        if isinstance(value, dict):
            return schema.required_fields <= value.keys() and all(
                self.matches(child, value[name], depth + 1)
                for name, child in schema.properties.items()
                if name in value
            )
        if isinstance(value, list) and schema.items is not None:
            return all(self.matches(schema.items, item, depth + 1) for item in value)
        return True

    def build(self, schema: SchemaSpec) -> JSONValue:
        self.prepare(schema)
        return self._build(schema, 0)

    def _build(self, schema: SchemaSpec, depth: int) -> JSONValue:
        self.step(depth)
        choices = self._enums[id(schema)]
        if choices:
            for value in choices:
                if self.matches(schema, value, depth):
                    return self.clone(value, depth)
            raise NoExampleError("Ни одно значение enum не удовлетворяет ограничениям")
        kinds = [schema.schema_type]
        if schema.schema_type == "any":
            kinds = ["object"] if schema.properties or schema.required_fields else []
            if schema.items is not None:
                kinds.append("array")
            kinds.append("null")
        elif schema.nullable and schema.schema_type != "null":
            kinds.append("null")
        for kind in kinds:
            try:
                value = self._make(kind, schema, depth)
            except NoExampleError:
                continue
            if self.matches(schema, value, depth):
                return value
        raise NoExampleError("Не удалось построить значение для схемы")

    def _make(self, kind: str, schema: SchemaSpec, depth: int) -> JSONValue:
        if kind == "null":
            return None
        if kind == "boolean":
            return False
        if kind in {"integer", "number"}:
            minimum, maximum = schema.minimum, schema.maximum
            if minimum is not None and maximum is not None and minimum > maximum:
                raise NoExampleError("Противоречивые числовые границы")
            if kind == "integer":
                low = math.ceil(minimum) if minimum is not None else None
                high = math.floor(maximum) if maximum is not None else None
                if low is not None and high is not None and low > high:
                    raise NoExampleError("В интервале нет целых чисел")
                if low is not None and low > 0:
                    return low
                return high if high is not None and high < 0 else 0
            if minimum is not None and minimum > 0:
                return minimum
            return maximum if maximum is not None and maximum < 0 else 0
        if kind == "string":
            low = schema.min_length or 0
            high = schema.max_length
            if high is not None and low > high:
                raise NoExampleError("Противоречивые границы длины строки")
            return self.text(max(1, low) if high != 0 else 0)
        if kind == "array":
            if schema.items is None:
                return []
            try:
                return [self._build(schema.items, depth + 1)]
            except NoExampleError:
                return []
        result: dict[str, JSONValue] = {}
        for name, child in schema.properties.items():
            self.check_string(name)
            try:
                result[name] = self._build(child, depth + 1)
            except NoExampleError:
                if name in schema.required_fields:
                    raise
        for name in sorted(schema.required_fields - result.keys()):
            self.step(depth + 1)
            self.check_string(name)
            result[name] = None
        return result
