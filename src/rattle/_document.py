"""JSON-совместимая загрузка OpenAPI без неявных правил YAML 1.1."""

import json
import math
import re
from collections.abc import Hashable

import yaml
from yaml.nodes import MappingNode, ScalarNode

from rattle.exceptions import InvalidOpenAPISpecError

_NULL = re.compile(r"(?:~|null|Null|NULL|)\Z")
_BOOL = re.compile(r"(?:true|True|TRUE|false|False|FALSE)\Z")
_INT = re.compile(r"[-+]?(?:[0-9]+|0o[0-7]+|0x[0-9a-fA-F]+)\Z")
_FLOAT = re.compile(
    r"(?:[-+]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][-+]?[0-9]+)?"
    r"|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))\Z"
)
_TAG = "tag:yaml.org,2002:"
_JSON_TAGS = frozenset(
    _TAG + name for name in ("null", "bool", "int", "float", "str", "seq", "map")
)


def load_document(text: str) -> object:
    """Отклоняет дубли и не-JSON числа до разбора схем, в том числе в аннотациях."""
    try:
        document: object
        try:
            document = json.loads(
                text,
                object_pairs_hook=_unique_object,
                parse_float=_finite_float,
                parse_constant=_reject_constant,
            )
        except json.JSONDecodeError:
            document = yaml.load(text, Loader=_OpenAPILoader)
        return document
    except (yaml.YAMLError, ValueError) as error:
        raise InvalidOpenAPISpecError("Некорректный JSON или YAML в OpenAPI-схеме") from error


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise InvalidOpenAPISpecError(f"Повторяющийся ключ: {key}")
        result[key] = value
    return result


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise InvalidOpenAPISpecError(f"Число должно быть конечным: {value}")
    return number


def _reject_constant(value: str) -> object:
    raise InvalidOpenAPISpecError(f"Недопустимое JSON-число: {value}")


class _OpenAPILoader(yaml.SafeLoader):
    """Отдельные таблицы правил: глобальный yaml.safe_load не изменяется."""

    def construct_mapping(self, node: MappingNode, deep: bool = False) -> dict[Hashable, object]:
        if not isinstance(node, MappingNode):
            raise InvalidOpenAPISpecError("YAML mapping должен быть объектом")
        result: dict[Hashable, object] = {}
        for key_node, value_node in node.value:
            key: object = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str):
                raise InvalidOpenAPISpecError("YAML: ключи объектов должны быть строками")
            if key in result:
                raise InvalidOpenAPISpecError(f"Повторяющийся ключ: {key}")
            result[key] = self.construct_object(value_node, deep=deep)
        return result

    def _scalar(self, node: ScalarNode, pattern: re.Pattern[str]) -> str:
        value = self.construct_scalar(node)
        if pattern.fullmatch(value) is None:
            raise InvalidOpenAPISpecError(f"Недопустимое YAML-значение для {node.tag}")
        return value

    def _null(self, node: ScalarNode) -> None:
        self._scalar(node, _NULL)
        return None

    def _boolean(self, node: ScalarNode) -> bool:
        return self._scalar(node, _BOOL).lower() == "true"

    def _integer(self, node: ScalarNode) -> int:
        value = self._scalar(node, _INT)
        unsigned = value.lstrip("+-")
        base = 8 if unsigned.startswith("0o") else 16 if unsigned.startswith("0x") else 10
        return int(value, base)

    def _float(self, node: ScalarNode) -> float:
        value = self._scalar(node, _FLOAT)
        if value.lower().lstrip("+-") in {".inf", ".nan"}:
            raise InvalidOpenAPISpecError(f"Число должно быть конечным: {value}")
        return _finite_float(value)


_OpenAPILoader.yaml_implicit_resolvers = {}
_OpenAPILoader.yaml_constructors = {
    tag: constructor
    for tag, constructor in yaml.SafeLoader.yaml_constructors.items()
    if tag in _JSON_TAGS or tag is None
}
for name, pattern, first in (
    ("null", _NULL, ["~", "n", "N", ""]),
    ("bool", _BOOL, list("tTfF")),
    ("int", _INT, list("-+0123456789")),
    ("float", _FLOAT, list("-+0123456789.")),
):
    _OpenAPILoader.add_implicit_resolver(_TAG + name, pattern, first)
_OpenAPILoader.add_constructor(_TAG + "null", _OpenAPILoader._null)
_OpenAPILoader.add_constructor(_TAG + "bool", _OpenAPILoader._boolean)
_OpenAPILoader.add_constructor(_TAG + "int", _OpenAPILoader._integer)
_OpenAPILoader.add_constructor(_TAG + "float", _OpenAPILoader._float)
