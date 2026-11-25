from dataclasses import asdict

import pytest

from rattle.exceptions import (
    GenerationLimitError,
    NoExampleError,
    RequestGenerationError,
    UnsupportedSchemaError,
)
from rattle.requests import MutationCase, RequestData, RequestParameter


def test_absent_body_and_json_null_are_distinct() -> None:
    absent = RequestData("POST", "/items")
    null = RequestData("POST", "/items", body=None, has_body=True, content_type="application/json")

    assert absent != null
    assert absent.has_body is False
    assert null.has_body is True
    assert asdict(null)["body"] is None


def test_request_keeps_unserialized_parameter_data_and_settings() -> None:
    parameter = RequestParameter("id", "path", [1, 2], "simple", True)
    request = RequestData("GET", "/items/{id}", parameters=(parameter,))

    assert request.path == "/items/{id}"
    assert request.parameters[0].value == [1, 2]
    assert request.parameters[0].explode is True


def test_mutation_metadata_uses_an_unambiguous_path() -> None:
    case = MutationCase(RequestData("POST", "/items"), "wrong_type", "body", ("a/b~", 0, "id"))
    assert case.path == ("a/b~", 0, "id")
    assert case.kind == "wrong_type"


@pytest.mark.parametrize("error", [UnsupportedSchemaError, NoExampleError, GenerationLimitError])
def test_generation_failures_have_a_common_exception(error: type[RequestGenerationError]) -> None:
    with pytest.raises(RequestGenerationError):
        raise error("reason")
