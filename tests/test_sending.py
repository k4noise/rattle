import gzip
import socket
from collections.abc import Iterator
from typing import cast
from urllib.parse import parse_qsl

import httpx
import pytest

from rattle.exceptions import SenderStateError, TargetUnavailableError
from rattle.generation import RequestGenerator
from rattle.http_models import SendResult
from rattle.models import EndpointSpec, HttpMethod, ParameterSpec, SchemaSpec
from rattle.requests import JSONValue, MutationCase, RequestData, RequestParameter
from rattle.sending import HTTPSender

BASE = "https://api.example.test/api"
PROBE = "https://api.example.test/ready"


def _case(data: RequestData | None = None) -> MutationCase:
    return MutationCase(data or RequestData("GET", "/items"), "wrong_type", "query", ("q",))


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("Tests must not connect to a real server")

    monkeypatch.setattr(socket.socket, "connect", forbidden)


class TrackedStream(httpx.SyncByteStream):
    def __init__(
        self, error: httpx.RequestError | None = None, chunks: tuple[bytes, ...] = (b"prefix",)
    ) -> None:
        self.error, self.chunks = error, chunks
        self.closed = False
        self.read_chunks = 0

    def __iter__(self) -> Iterator[bytes]:
        for chunk in self.chunks:
            self.read_chunks += 1
            yield chunk
        if self.error is not None:
            raise self.error

    def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize("status", [200, 301, 400, 404, 499, 500, 599, 600])
def test_status_classification_does_not_raise_for_4xx(status: int) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(status, content=b"body"))
    case = _case()
    with HTTPSender(BASE, transport=transport) as sender:
        result = sender.send(case)
    assert result.case is case
    assert result.is_crash is (500 <= status <= 599)
    assert result.outcome == ("crash" if result.is_crash else "response")
    assert result.response is not None and result.response.status_code == status
    assert result.response.content == b"body" and result.response.complete is True
    assert result.error is None and result.availability is None
    assert result.request is not None and result.request.url == BASE + "/items"


def test_absent_and_null_bodies_and_actual_transport_headers_are_recorded() -> None:
    received: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        received.append(request)
        return httpx.Response(200)

    with HTTPSender(BASE, transport=httpx.MockTransport(handler)) as sender:
        absent = sender.send(_case(RequestData("POST", "/items")))
        null = sender.send(_case(RequestData("POST", "/items", has_body=True)))
    assert [request.content for request in received] == [b"", b"null"]
    assert "content-type" not in received[0].headers
    assert received[1].headers["content-type"] == "application/json"
    assert absent.request is not None and absent.request.content is None
    assert null.request is not None and null.request.content == b"null"
    assert ("Host", "api.example.test") in null.request.headers
    assert ("Content-Length", "4") in null.request.headers


def test_missing_path_and_bad_headers_produce_results_without_any_io_or_probe() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200)

    missing = _case(RequestData("GET", "/items/{id}"))
    invalid = _case(
        RequestData(
            "GET", "/items", (RequestParameter("X-Name", "header", "\r\n", "simple", False),)
        )
    )
    with HTTPSender(
        BASE, availability_url=PROBE, max_unavailable=1, transport=httpx.MockTransport(handler)
    ) as sender:
        result = sender.send(missing)
        assert result.outcome == "unsendable" and result.error is not None
        assert result.request is None and result.response is None and result.availability is None
        result = sender.send(invalid)
        assert result.outcome == "serialization_error"
        assert result.request is None and result.response is None and result.availability is None
        assert sender.send(_case()).outcome == "response"
    assert calls == [BASE + "/items"]


@pytest.mark.parametrize("status", [200, 404, 405, 503])
def test_only_an_explicit_complete_probe_confirms_an_interrupted_response(status: int) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if str(request.url) == PROBE:
            return httpx.Response(status, content=b"ok")
        return httpx.Response(200, stream=TrackedStream(httpx.ReadError("response interrupted")))

    with HTTPSender(BASE, availability_url=PROBE, transport=httpx.MockTransport(handler)) as sender:
        result = sender.send(_case(RequestData("POST", "/items", has_body=True)))
    assert result.outcome == "crash"
    assert result.response is not None and not result.response.complete
    assert result.error is not None and result.error.kind == "response"
    assert result.availability is not None and result.availability.available is True
    assert result.availability.status_code == status
    assert [(request.method, str(request.url)) for request in calls] == [
        ("POST", BASE + "/items"),
        ("GET", PROBE),
    ]
    assert calls[1].content == b"" and "content-type" not in calls[1].headers


def test_interruption_keeps_partial_response_and_closes_its_stream() -> None:
    broken = TrackedStream(httpx.ReadError("interrupted"))

    def handler(request: httpx.Request) -> httpx.Response:
        return (
            httpx.Response(200, content=b"ok")
            if str(request.url) == PROBE
            else httpx.Response(200, stream=broken)
        )

    with HTTPSender(BASE, availability_url=PROBE, transport=httpx.MockTransport(handler)) as sender:
        result = sender.send(_case())
    assert result.is_crash is True
    assert result.response is not None
    assert result.response.content == b"prefix"
    assert result.response.complete is result.response.truncated is False
    assert broken.closed is True


def test_no_probe_url_means_no_guessed_health_request_or_confirmed_crash() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        raise httpx.ReadError("closed")

    with HTTPSender(BASE, transport=httpx.MockTransport(handler)) as sender:
        result = sender.send(_case())
    assert result.outcome == "network_error" and result.is_crash is False
    assert result.availability is None
    assert calls == [BASE + "/items"]


@pytest.mark.parametrize(
    "error_type",
    [
        httpx.ConnectError,
        httpx.ConnectTimeout,
        httpx.ReadTimeout,
        httpx.WriteTimeout,
        httpx.PoolTimeout,
        httpx.WriteError,
        httpx.CloseError,
    ],
)
def test_timeouts_and_non_read_failures_are_not_crashes_even_with_a_live_probe(
    error_type: type[httpx.RequestError],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == PROBE:
            return httpx.Response(200)
        raise error_type("network failure")

    with HTTPSender(
        BASE, availability_url=PROBE, max_unavailable=1, transport=httpx.MockTransport(handler)
    ) as sender:
        results = [sender.send(_case()) for _ in range(3)]
    assert all(result.outcome == "network_error" and not result.is_crash for result in results)
    assert all(
        result.availability is not None and result.availability.available for result in results
    )


@pytest.mark.parametrize(
    ("error_type", "kind"),
    [
        (httpx.ReadTimeout, "timeout"),
        (httpx.ReadError, "response"),
        (httpx.RemoteProtocolError, "protocol"),
        (httpx.LocalProtocolError, "serialization"),
        (httpx.CloseError, "network"),
    ],
)
def test_known_5xx_is_a_crash_regardless_of_a_later_body_error(
    error_type: type[httpx.RequestError],
    kind: str,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, stream=TrackedStream(error_type("body error")))

    with HTTPSender(
        BASE, availability_url=PROBE, max_unavailable=1, transport=httpx.MockTransport(handler)
    ) as sender:
        result = sender.send(_case())
        assert sender.send(_case()).is_crash
    assert result.is_crash and result.error is not None and result.error.kind == kind
    assert result.response is not None and result.response.status_code == 503
    assert result.availability is None and calls == 2


def test_unavailability_keeps_last_result_without_consuming_the_next_case() -> None:
    consumed: list[int] = []
    calls: list[str] = []

    def cases() -> Iterator[MutationCase]:
        for index in range(5):
            consumed.append(index)
            yield _case(RequestData("GET", f"/items/{index}"))

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        raise httpx.ConnectError("unreachable")

    with HTTPSender(BASE, max_unavailable=3, transport=httpx.MockTransport(handler)) as sender:
        iterator = sender.send_all(cases())
        assert consumed == [] and calls == []
        assert next(iterator).outcome == "network_error"
        assert next(iterator).outcome == "network_error"
        with pytest.raises(TargetUnavailableError) as caught:
            next(iterator)
        assert caught.value.consecutive_failures == 3
        assert caught.value.last_result.case.request.path == "/items/2"
        assert caught.value.last_result.outcome == "network_error"
        with pytest.raises(SenderStateError):
            sender.send(_case())
    assert consumed == [0, 1, 2]
    assert calls == [BASE + f"/items/{index}" for index in range(3)]


def test_failed_probe_does_not_turn_interruption_into_crash() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if str(request.url) == PROBE:
            raise httpx.ConnectError("probe unreachable")
        raise httpx.ReadError("interrupted")

    with HTTPSender(
        BASE, availability_url=PROBE, max_unavailable=1, transport=httpx.MockTransport(handler)
    ) as sender:
        with pytest.raises(TargetUnavailableError) as caught:
            sender.send(_case())
    result = caught.value.last_result
    assert result.outcome == "network_error"
    assert result.availability is not None and result.availability.available is False
    assert result.availability.error is not None and result.availability.error.kind == "connection"
    assert calls == [BASE + "/items", PROBE]


def test_success_resets_failure_count_but_unsendable_cases_do_not() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            return httpx.Response(404)
        raise httpx.ConnectError("unreachable")

    with HTTPSender(BASE, max_unavailable=2, transport=httpx.MockTransport(handler)) as sender:
        assert sender.send(_case()).outcome == "network_error"
        assert sender.send(_case()).outcome == "response"
        assert sender.send(_case()).outcome == "network_error"
        assert sender.send(_case(RequestData("GET", "/{id}"))).outcome == "unsendable"
        with pytest.raises(TargetUnavailableError):
            sender.send(_case())
    assert attempts == 4


def test_request_and_probe_timeouts_are_independent() -> None:
    timeouts: list[dict[str, float | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        timeouts.append(request.extensions["timeout"])
        if str(request.url) != PROBE:
            raise httpx.ReadTimeout("timeout")
        return httpx.Response(200)

    with HTTPSender(
        BASE,
        availability_url=PROBE,
        timeout=0.5,
        availability_timeout=0.1,
        transport=httpx.MockTransport(handler),
    ) as sender:
        sender.send(_case())
    assert timeouts == [
        dict.fromkeys(["connect", "read", "write", "pool"], 0.5),
        dict.fromkeys(["connect", "read", "write", "pool"], 0.1),
    ]


def test_redirects_and_automatic_cookie_state_do_not_change_later_cases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://not-a-real-proxy.example.test:9999")
    received: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        received.append(request)
        return httpx.Response(
            302,
            headers={
                "Location": "https://elsewhere.example.test/",
                "Set-Cookie": "state=leak; Path=/",
            },
        )

    data = RequestData(
        "GET", "/items", (RequestParameter("sid", "cookie", "explicit", "form", True),)
    )
    with HTTPSender(BASE, transport=httpx.MockTransport(handler)) as sender:
        first = sender.send(_case(data))
        second = sender.send(_case())
    assert first.response is not None and first.response.status_code == 302
    assert second.outcome == "response"
    assert len(received) == 2
    assert received[0].headers["cookie"] == "sid=explicit"
    assert "cookie" not in received[1].headers
    assert all(request.url.host == "api.example.test" for request in received)


@pytest.mark.parametrize("status", [200, 500])
def test_large_response_is_bounded_and_closed_without_discarding_status(status: int) -> None:
    stream = TrackedStream(chunks=(b"abcdef", b"never read"))
    with HTTPSender(
        BASE,
        max_response_bytes=3,
        transport=httpx.MockTransport(lambda request: httpx.Response(status, stream=stream)),
    ) as sender:
        result = sender.send(_case())
    assert result.is_crash is (status == 500)
    assert result.response is not None and result.response.content == b"abc"
    assert result.response.truncated is True and result.response.complete is False
    assert stream.closed is True and stream.read_chunks == 1


def test_probe_requires_a_complete_bounded_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == PROBE:
            return httpx.Response(200, stream=TrackedStream(chunks=(b"oversized",)))
        raise httpx.ReadError("interrupted")

    with HTTPSender(
        BASE, availability_url=PROBE, max_response_bytes=3, transport=httpx.MockTransport(handler)
    ) as sender:
        result = sender.send(_case())
    assert result.outcome == "network_error"
    assert result.availability is not None and not result.availability.available
    assert result.availability.status_code == 200
    assert result.availability.error is not None and result.availability.error.kind == "probe_limit"


def test_raw_body_is_not_decompressed_and_exact_size_is_complete() -> None:
    raw = gzip.compress(b"x" * 10000)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"Content-Encoding": "gzip"}, stream=TrackedStream(chunks=(raw,))
        )

    with HTTPSender(
        BASE, max_response_bytes=len(raw), transport=httpx.MockTransport(handler)
    ) as sender:
        result = sender.send(_case())
    assert result.response is not None and result.response.content == raw
    assert result.response.complete is True and result.response.truncated is False


class ClosingTransport(httpx.MockTransport):
    def __init__(self, fail: bool = False) -> None:
        self.closed = False
        self.fail = fail
        super().__init__(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.fail:
            raise RuntimeError("programming error, not an API crash")
        return httpx.Response(200)

    def close(self) -> None:
        self.closed = True


def test_context_manager_is_required_and_closes_on_normal_and_exceptional_exit() -> None:
    transport = ClosingTransport()
    sender = HTTPSender(BASE, transport=transport)
    with pytest.raises(SenderStateError):
        sender.send(_case())
    with sender:
        with pytest.raises(SenderStateError):
            sender.__enter__()
        assert sender.send(_case()).outcome == "response"
    assert transport.closed is True
    with pytest.raises(SenderStateError):
        sender.send(_case())
    with pytest.raises(SenderStateError):
        sender.__enter__()
    broken = ClosingTransport(fail=True)
    with (
        pytest.raises(RuntimeError, match="programming error"),
        HTTPSender(BASE, transport=broken) as other,
    ):
        other.send(_case())
    assert broken.closed is True


def test_closed_sender_does_not_pull_another_item_from_a_paused_iterator() -> None:
    consumed: list[int] = []

    def cases() -> Iterator[MutationCase]:
        for index in range(2):
            consumed.append(index)
            yield _case()

    with HTTPSender(
        BASE, transport=httpx.MockTransport(lambda request: httpx.Response(200))
    ) as sender:
        iterator = sender.send_all(cases())
        next(iterator)
    with pytest.raises(SenderStateError):
        next(iterator)
    assert consumed == [0]


def test_generator_cases_are_not_changed_and_results_keep_their_mutation() -> None:
    endpoint = EndpointSpec(
        "GET",
        "/items/{id}",
        (ParameterSpec("id", "path", True, SchemaSpec(schema_type="integer", minimum=1)),),
    )
    generator = RequestGenerator()
    cases = list(generator.generate(endpoint))
    before = list(generator.generate(endpoint))
    with HTTPSender(
        BASE, transport=httpx.MockTransport(lambda request: httpx.Response(400))
    ) as sender:
        results: list[SendResult] = list(sender.send_all(cases))
    assert cases == before
    assert [result.case for result in results] == cases
    assert results[0].outcome == "unsendable"
    assert all(result.outcome == "response" for result in results[1:])
    assert all(not result.is_crash for result in results)


@pytest.mark.parametrize(
    "availability_url",
    [
        "https://other.example.test/ready",
        "http://api.example.test/ready",
        "https://api.example.test:444/ready",
        "/ready",
        "https://api.example.test/ready#fragment",
    ],
)
def test_probe_url_must_be_explicit_absolute_and_same_origin(availability_url: str) -> None:
    with pytest.raises(ValueError):
        HTTPSender(BASE, availability_url=availability_url)


@pytest.mark.parametrize("option", ["timeout", "availability_timeout"])
@pytest.mark.parametrize("value", [0, -1, True, float("inf"), float("nan")])
def test_timeout_configuration_is_bounded(option: str, value: float) -> None:
    with pytest.raises(ValueError):
        if option == "timeout":
            HTTPSender(BASE, timeout=value)
        else:
            HTTPSender(BASE, availability_timeout=value)


@pytest.mark.parametrize("option", ["max_unavailable", "max_response_bytes", "max_request_bytes"])
@pytest.mark.parametrize("value", [0, -1, True])
def test_integer_limits_are_validated(option: str, value: int) -> None:
    with pytest.raises(ValueError):
        if option == "max_unavailable":
            HTTPSender(BASE, max_unavailable=value)
        elif option == "max_response_bytes":
            HTTPSender(BASE, max_response_bytes=value)
        else:
            HTTPSender(BASE, max_request_bytes=value)


def test_non_numeric_timeout_is_a_configuration_error() -> None:
    with pytest.raises(ValueError):
        HTTPSender(BASE, timeout=cast(float, "five"))


@pytest.mark.parametrize("error_type", [httpx.LocalProtocolError, httpx.UnsupportedProtocol])
def test_local_failures_are_serialization_errors_without_a_probe_or_failure_count(
    error_type: type[httpx.RequestError],
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise error_type("cannot send this request")

    with HTTPSender(
        BASE, availability_url=PROBE, max_unavailable=1, transport=httpx.MockTransport(handler)
    ) as sender:
        for _ in range(3):
            result = sender.send(_case())
            assert result.outcome == "serialization_error"
            assert result.error is not None and result.error.kind == "serialization"
            assert result.request is not None and result.availability is None
    assert calls == 3


def test_successful_run_is_lazy_and_consumes_cases_only_after_the_previous_result() -> None:
    events: list[str] = []

    def cases() -> Iterator[MutationCase]:
        for index in range(3):
            events.append(f"generate {index}")
            yield _case(RequestData("GET", f"/items/{index}"))

    def handler(request: httpx.Request) -> httpx.Response:
        events.append("send " + request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(422)

    with HTTPSender(BASE, transport=httpx.MockTransport(handler)) as sender:
        results = sender.send_all(cases())
        assert events == []
        for index in range(3):
            assert next(results).outcome == "response"
            assert events == [
                event
                for number in range(index + 1)
                for event in (
                    f"generate {number}",
                    f"send {number}",
                )
            ]
        with pytest.raises(StopIteration):
            next(results)


def test_failed_probe_stream_is_not_a_confirmation_and_both_streams_close() -> None:
    main = TrackedStream(httpx.RemoteProtocolError("main interrupted"))
    probe = TrackedStream(httpx.ReadError("probe interrupted"))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=probe if str(request.url) == PROBE else main)

    with HTTPSender(BASE, availability_url=PROBE, transport=httpx.MockTransport(handler)) as sender:
        result = sender.send(_case())
    assert result.outcome == "network_error"
    assert result.availability is not None and result.availability.available is False
    assert result.availability.status_code == 200
    assert result.availability.error is not None and result.availability.error.kind == "response"
    assert main.closed is probe.closed is True


def test_probe_is_independent_of_mutation_data_and_does_not_follow_redirect() -> None:
    calls: list[httpx.Request] = []
    probe_url = PROBE + "?check=ready"

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if str(request.url) == probe_url:
            return httpx.Response(302, headers={"Location": "https://elsewhere.example.test/"})
        raise httpx.RemoteProtocolError("interrupted")

    parameters = (
        RequestParameter("Authorization", "header", "sensitive", "simple", False),
        RequestParameter("session", "cookie", "sensitive", "form", True),
        RequestParameter("mutated", "query", "bad", "form", True),
    )
    data = RequestData("PATCH", "/items", parameters, body={"bad": True}, has_body=True)
    with HTTPSender(
        BASE, availability_url=probe_url, transport=httpx.MockTransport(handler)
    ) as sender:
        result = sender.send(_case(data))
    assert result.outcome == "network_error" and len(calls) == 2
    assert result.availability is not None and result.availability.available is True
    assert result.availability.status_code == 302
    assert calls[1].method == "GET" and str(calls[1].url) == probe_url
    assert calls[1].content == b""
    assert set(calls[1].headers) == {"host"}


def test_context_closes_transport_when_unavailability_exception_leaves_the_block() -> None:
    class UnreachableTransport(ClosingTransport):
        def handle(self, request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("unreachable")

    transport = UnreachableTransport()
    with pytest.raises(TargetUnavailableError) as caught:
        with HTTPSender(BASE, max_unavailable=1, transport=transport) as sender:
            sender.send(_case())
    assert transport.closed is True
    assert caught.value.last_result.outcome == "network_error"


def test_wire_snapshots_cannot_change_after_the_source_case_is_edited() -> None:
    body: dict[str, JSONValue] = {"key": [1]}
    values: list[JSONValue] = ["one", "two"]
    data = RequestData(
        "PUT",
        "/items",
        (RequestParameter("q", "query", values, "form", True),),
        body=body,
        has_body=True,
    )
    with HTTPSender(
        BASE, transport=httpx.MockTransport(lambda request: httpx.Response(200))
    ) as sender:
        result = sender.send(_case(data))
        before = result.request
        assert before is not None and before.content == b'{"key":[1]}'
        values.append("third")
        body["key"] = None
        later = sender.send(_case(data))
    assert result.request is before
    assert before.content == b'{"key":[1]}' and before.url.endswith("?q=one&q=two")
    assert later.request is not None and later.request.content == b'{"key":null}'
    assert later.request.url.endswith("?q=one&q=two&q=third")


def test_get_and_delete_may_have_a_body_and_latin1_headers_keep_actual_bytes() -> None:
    received: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        received.append(request)
        return httpx.Response(200)

    parameter = RequestParameter("X-Word", "header", "café", "simple", False)
    methods: tuple[HttpMethod, ...] = ("GET", "DELETE")
    with HTTPSender(BASE, transport=httpx.MockTransport(handler)) as sender:
        for method in methods:
            result = sender.send(
                _case(RequestData(method, "/items", (parameter,), body=1, has_body=True))
            )
            assert result.request is not None and result.request.content == b"1"
            assert ("X-Word", "café") in result.request.headers
    assert all((b"X-Word", b"caf\xe9") in request.headers.raw for request in received)


@pytest.mark.parametrize(
    "probe_url",
    [
        PROBE + "/../other",
        PROBE + "?x=%ZZ",
        PROBE + "%?2F",
        PROBE + "%2?F=1",
    ],
)
def test_probe_path_is_not_silently_normalized(probe_url: str) -> None:
    with pytest.raises(ValueError):
        HTTPSender(BASE, availability_url=probe_url)


def test_default_and_explicit_same_origin_ports_are_equivalent() -> None:
    HTTPSender(BASE, availability_url="https://api.example.test:443/ready")


def test_request_size_limit_includes_headers_added_by_the_http_client() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200)

    with HTTPSender(BASE, max_request_bytes=50, transport=httpx.MockTransport(handler)) as sender:
        result = sender.send(_case())
    assert result.outcome == "serialization_error" and result.request is None
    assert calls == []


@pytest.mark.parametrize(
    "value",
    [
        "a&admin=true",
        "a+b",
        "a;b=c",
        "a#fragment",
        ["one,two"],
        {"key": "one,two"},
    ],
)
def test_ambiguous_reserved_query_is_unsendable_without_sending_or_probing(
    value: JSONValue,
) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        assert parse_qsl(request.url.query.decode("ascii")) == [("q", "a&admin=true+b")]
        return httpx.Response(422)

    case = _case(
        RequestData("GET", "/items", (RequestParameter("q", "query", value, "form", False, True),))
    )
    with HTTPSender(
        BASE, availability_url=PROBE, max_unavailable=1, transport=httpx.MockTransport(handler)
    ) as sender:
        result = sender.send(case)
        assert result.case is case and result.outcome == "unsendable"
        assert result.error is not None and result.error.kind == "unsendable"
        assert result.request is None and result.response is None and result.availability is None
        assert calls == []
        control = _case(
            RequestData(
                "GET", "/items", (RequestParameter("q", "query", "a&admin=true+b", "form", True),)
            )
        )
        assert sender.send(control).outcome == "response"
    assert len(calls) == 1


@pytest.mark.parametrize(
    "message",
    [
        "illegal status line: bytearray(b'NOT HTTP')",
        "Server disconnected without sending a response.",
        "response interrupted",
    ],
)
def test_remote_protocol_error_before_response_is_not_a_crash_with_a_live_probe(
    message: str,
) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if str(request.url) == PROBE:
            return httpx.Response(200)
        raise httpx.RemoteProtocolError(message)

    with HTTPSender(
        BASE, availability_url=PROBE, max_unavailable=1, transport=httpx.MockTransport(handler)
    ) as sender:
        result = sender.send(_case())
    assert result.outcome == "network_error" and not result.is_crash
    assert result.response is None and result.request is not None
    assert result.error is not None and result.error.kind == "protocol"
    assert result.error.message == message
    assert result.availability is not None and result.availability.available is True
    assert calls == [BASE + "/items", PROBE]


@pytest.mark.parametrize(
    "message",
    [
        "illegal chunk header: bytearray(b'not a chunk size')",
        "peer closed connection without sending complete message body",
    ],
)
@pytest.mark.parametrize("status", [200, 400])
@pytest.mark.parametrize("headers", [{"Transfer-Encoding": "chunked"}, {"Content-Length": "10"}])
def test_remote_protocol_error_without_transport_eof_evidence_is_ambiguous(
    message: str,
    status: int,
    headers: dict[str, str],
) -> None:
    broken = TrackedStream(httpx.RemoteProtocolError(message))

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == PROBE:
            return httpx.Response(200)
        return httpx.Response(status, headers=headers, stream=broken)

    with HTTPSender(
        BASE, availability_url=PROBE, max_unavailable=1, transport=httpx.MockTransport(handler)
    ) as sender:
        result = sender.send(_case())
    assert result.outcome == "network_error" and not result.is_crash
    assert result.response is not None and result.response.status_code == status
    assert result.response.content == b"prefix" and result.response.complete is False
    assert result.response.peer_eof is False
    assert result.error is not None and result.error.kind == "protocol"
    assert result.error.message == message
    assert result.availability is not None and result.availability.available is True
    assert broken.closed is True


def test_read_error_before_a_valid_response_is_not_proof_of_response_interruption() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == PROBE:
            return httpx.Response(200)
        raise httpx.ReadError("connection reset before response headers")

    with HTTPSender(BASE, availability_url=PROBE, transport=httpx.MockTransport(handler)) as sender:
        result = sender.send(_case())
    assert result.outcome == "network_error" and not result.is_crash
    assert result.response is None
    assert result.error is not None and result.error.kind == "response"
    assert result.availability is not None and result.availability.available is True


@pytest.mark.parametrize("chunks", [(), (b"prefix",)])
def test_read_error_after_response_headers_can_be_confirmed_as_a_crash(
    chunks: tuple[bytes, ...],
) -> None:
    broken = TrackedStream(httpx.ReadError("connection reset"), chunks=chunks)

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == PROBE:
            return httpx.Response(200)
        return httpx.Response(200, stream=broken)

    with HTTPSender(BASE, availability_url=PROBE, transport=httpx.MockTransport(handler)) as sender:
        result = sender.send(_case())
    assert result.is_crash and result.error is not None and result.error.kind == "response"
    assert result.response is not None and result.response.complete is False
    assert result.response.content == b"".join(chunks)
    assert result.availability is not None and result.availability.available is True
    assert broken.closed is True


def test_local_truncation_and_a_close_error_are_not_an_api_crash() -> None:
    class BrokenCloseStream(TrackedStream):
        def close(self) -> None:
            super().close()
            raise httpx.ReadError("failed to close a locally truncated response")

    stream = BrokenCloseStream(chunks=(b"abcdef",))

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == PROBE:
            return httpx.Response(200, content=b"ok")
        return httpx.Response(200, stream=stream)

    with HTTPSender(
        BASE, availability_url=PROBE, max_response_bytes=3, transport=httpx.MockTransport(handler)
    ) as sender:
        result = sender.send(_case())
    assert result.outcome == "network_error" and not result.is_crash
    assert result.response is not None and result.response.content == b"abc"
    assert result.response.truncated is True and result.response.complete is False
    assert result.availability is not None and result.availability.available is True
    assert stream.closed is True


def test_a_protocol_error_in_the_probe_cannot_confirm_a_main_read_error() -> None:
    broken = TrackedStream(httpx.ReadError("main read interrupted"))

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == PROBE:
            raise httpx.RemoteProtocolError("illegal status line")
        return httpx.Response(200, stream=broken)

    with HTTPSender(BASE, availability_url=PROBE, transport=httpx.MockTransport(handler)) as sender:
        result = sender.send(_case())
    assert result.outcome == "network_error" and not result.is_crash
    assert result.response is not None and not result.response.complete
    assert result.availability is not None and not result.availability.available
    assert result.availability.error is not None and result.availability.error.kind == "protocol"
    assert broken.closed is True
