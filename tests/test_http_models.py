from dataclasses import asdict

import pytest

from rattle.exceptions import (
    RequestSendingError,
    RequestSerializationError,
    SenderStateError,
    TargetUnavailableError,
    UnsendableRequestError,
)
from rattle.http_models import HTTPResponseData, SendError, SendResult, SerializedRequest
from rattle.requests import JSONValue, MutationCase, RequestData


def test_wire_snapshots_distinguish_absent_body_and_json_null() -> None:
    absent = SerializedRequest("POST", "https://api.example.test/items")
    null = SerializedRequest("POST", "https://api.example.test/items", content=b"null")
    assert absent.content is None and null.content == b"null"
    assert absent != null


def test_result_keeps_mutation_association_and_independent_wire_data() -> None:
    data: dict[str, JSONValue] = {"id": 1}
    case = MutationCase(
        RequestData("POST", "/", body=data, has_body=True), "wrong_type", "body", ("id",)
    )
    request = SerializedRequest("POST", "https://api.example.test/", content=b'{"id":1}')
    result = SendResult(case, "crash", request, HTTPResponseData(500))
    data["id"] = 99
    assert result.case is case
    assert result.request is not None and result.request.content == b'{"id":1}'
    assert result.is_crash is True
    assert asdict(result)["response"]["status_code"] == 500


def test_partial_response_and_network_error_are_both_recorded() -> None:
    case = MutationCase(RequestData("GET", "/"), "missing_required", "query", ("q",))
    response = HTTPResponseData(200, content=b"partial", complete=False)
    result = SendResult(
        case, "network_error", response=response, error=SendError("response", "interrupted")
    )
    assert result.is_crash is False
    assert result.response is not None and result.response.complete is False
    assert result.error is not None and result.error.kind == "response"
    error = TargetUnavailableError(result, 3)
    assert error.last_result is result
    assert error.consecutive_failures == 3


@pytest.mark.parametrize(
    "error", [RequestSerializationError, UnsendableRequestError, SenderStateError]
)
def test_sender_failures_have_a_common_exception(error: type[RequestSendingError]) -> None:
    with pytest.raises(RequestSendingError):
        raise error("reason")
