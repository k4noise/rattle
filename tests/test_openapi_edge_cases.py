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


def test_utf8_bom_and_path_extensions_are_supported(parser: OpenAPIParser) -> None:
    raw = json.dumps({"openapi": "3.0.3", "paths": {"x-comment": "metadata", "/ping": {"get": {}}}})
    result = parser.parse(b"\xef\xbb\xbf" + raw.encode("utf-8"))

    assert result.endpoints[0].path == "/ping"


@pytest.mark.parametrize("version", ["3.broken", "3.0.3-extra", "3.2.0", "2.0"])
def test_only_the_documented_versions_are_accepted(parser: OpenAPIParser, version: str) -> None:
    raw = json.dumps({"openapi": version, "paths": {"/ping": {"get": {}}}})
    with pytest.raises(InvalidOpenAPISpecError, match="версии"):
        parser.parse(raw)


def test_unreadable_numeric_literal_is_a_spec_error(parser: OpenAPIParser) -> None:
    limit = sys.get_int_max_str_digits()
    if limit == 0:
        pytest.skip("Ограничение числовых литералов в интерпретаторе отключено")
    raw = _spec_with_body({"type": "integer", "minimum": "HUGE_NUMBER"})
    raw = raw.replace('"HUGE_NUMBER"', "9" * (limit + 1))

    with pytest.raises(InvalidOpenAPISpecError, match="JSON|YAML"):
        parser.parse(raw)
