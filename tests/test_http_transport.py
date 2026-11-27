"""Настоящий HTTP-парсер с in-memory NetworkBackend, без искусственных HTTPX-ошибок тела."""

import socket
import ssl
from collections.abc import Iterable

import httpcore
import httpx
import pytest

from rattle._http_transport import EOFTrackingTransport, SocketOption, peer_eof
from rattle.requests import MutationCase, RequestData
from rattle.sending import HTTPSender

_FIXED_EOF = b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\nConnection: close\r\n\r\nabc"
_READY = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"
_CLOSE_DELIMITED = b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\nabc"
_BAD_CHUNK = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\nXYZ\r\n"


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("Memory backend tests must not connect to any socket")

    monkeypatch.setattr(socket.socket, "connect", forbidden)


class RecordingStream(httpcore.MockStream):
    def __init__(self, replies: list[bytes]) -> None:
        super().__init__(list(replies))
        self.closed = False
        self.tls_names: list[str | None] = []
        self.read_timeouts: list[float | None] = []
        self.writes: list[bytes] = []

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        self.read_timeouts.append(timeout)
        return super().read(max_bytes, timeout=timeout)

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self.writes.append(buffer)
        super().write(buffer, timeout=timeout)

    def close(self) -> None:
        self.closed = True
        super().close()

    def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.NetworkStream:
        self.tls_names.append(server_hostname)
        return super().start_tls(ssl_context, server_hostname=server_hostname, timeout=timeout)


class MemoryBackend(httpcore.NetworkBackend):
    def __init__(self, replies: list[list[bytes]]) -> None:
        self.replies = list(replies)
        self.streams: list[RecordingStream] = []
        self.connect_timeouts: list[float | None] = []

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[SocketOption] | None = None,
    ) -> httpcore.NetworkStream:
        assert self.replies, "Unexpected connection or replay"
        self.connect_timeouts.append(timeout)
        stream = RecordingStream(self.replies.pop(0))
        self.streams.append(stream)
        return stream


def _case() -> MutationCase:
    return MutationCase(RequestData("GET", "/items"), "wrong_type", "query", ("q",))


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_h11_truncation_really_raises_remote_protocol_error_and_records_eof(scheme: str) -> None:
    backend = MemoryBackend([[_FIXED_EOF]])
    transport = EOFTrackingTransport(network_backend=backend)
    with httpx.Client(transport=transport, trust_env=False) as client:
        with client.stream("GET", scheme + "://api.example.test/items") as response:
            with pytest.raises(httpx.RemoteProtocolError) as caught:
                response.read()
            assert isinstance(caught.value.__cause__, httpcore.RemoteProtocolError)
            assert response.status_code == 200 and peer_eof(response) is True
    assert len(backend.streams) == 1 and backend.streams[0].closed
    assert backend.streams[0].tls_names == (["api.example.test"] if scheme == "https" else [])


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_eof_evidence_and_timeouts_survive_the_httpx_core_adapter(scheme: str) -> None:
    backend = MemoryBackend([[_FIXED_EOF], [_READY]])
    base = scheme + "://api.example.test"
    with HTTPSender(
        base,
        availability_url=base + "/ready",
        timeout=0.5,
        availability_timeout=0.25,
        transport=EOFTrackingTransport(network_backend=backend),
    ) as sender:
        result = sender.send(_case())
    assert result.is_crash
    assert result.response is not None and result.response.peer_eof
    assert result.response.content == b"abc"
    assert result.error is not None and result.error.kind == "protocol"
    assert result.availability is not None and result.availability.available
    assert backend.connect_timeouts == [0.5, 0.25]
    assert all(value == 0.5 for value in backend.streams[0].read_timeouts)
    assert all(value == 0.25 for value in backend.streams[1].read_timeouts)
    assert all(stream.closed for stream in backend.streams)
    assert [stream.writes[0].split(b"\r\n", 1)[0] for stream in backend.streams] == [
        b"GET /items HTTP/1.1",
        b"GET /ready HTTP/1.1",
    ]


def test_eof_from_a_previous_response_cannot_turn_another_protocol_error_into_a_crash() -> None:
    backend = MemoryBackend([[_CLOSE_DELIMITED], [_BAD_CHUNK], [_READY]])
    with HTTPSender(
        "http://api.example.test",
        availability_url="http://api.example.test/ready",
        transport=EOFTrackingTransport(network_backend=backend),
    ) as sender:
        first = sender.send(_case())
        second = sender.send(_case())
    assert first.outcome == "response" and first.response is not None and first.response.peer_eof
    assert second.outcome == "network_error" and not second.is_crash
    assert second.response is not None and not second.response.peer_eof
    assert second.availability is not None and second.availability.available
    assert len(backend.streams) == 3 and all(stream.closed for stream in backend.streams)


def test_eof_from_a_successful_probe_is_not_evidence_about_the_main_response() -> None:
    backend = MemoryBackend([[_BAD_CHUNK], [_CLOSE_DELIMITED]])
    with HTTPSender(
        "http://api.example.test",
        availability_url="http://api.example.test/ready",
        transport=EOFTrackingTransport(network_backend=backend),
    ) as sender:
        result = sender.send(_case())
    assert result.outcome == "network_error" and not result.is_crash
    assert result.response is not None and result.response.peer_eof is False
    assert result.availability is not None and result.availability.available


def test_keepalive_eof_evidence_is_specific_to_the_current_response() -> None:
    first_wire = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"
    backend = MemoryBackend([[first_wire, _FIXED_EOF]])
    with httpx.Client(transport=EOFTrackingTransport(network_backend=backend)) as client:
        first = client.get("http://api.example.test/first")
        assert first.content == b"ok" and peer_eof(first) is False
        with client.stream("GET", "http://api.example.test/second") as second:
            with pytest.raises(httpx.RemoteProtocolError):
                second.read()
            assert peer_eof(second) is True
        assert peer_eof(first) is False
    assert len(backend.streams) == 1 and backend.streams[0].closed


def test_keepalive_eof_before_next_response_headers_does_not_change_the_previous_evidence() -> None:
    backend = MemoryBackend([[b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"]])
    with httpx.Client(transport=EOFTrackingTransport(network_backend=backend)) as client:
        first = client.get("http://api.example.test/first")
        with pytest.raises(httpx.RemoteProtocolError):
            client.get("http://api.example.test/second")
        assert first.content == b"ok" and peer_eof(first) is False
    assert len(backend.streams) == 1 and backend.streams[0].closed


@pytest.mark.parametrize("has_body", [False, True])
def test_adapter_preserves_absent_body_versus_json_null_on_the_wire(has_body: bool) -> None:
    backend = MemoryBackend([[_READY]])
    case = MutationCase(RequestData("POST", "/items", has_body=has_body), "wrong_type", "body", ())
    with HTTPSender(
        "http://api.example.test", transport=EOFTrackingTransport(network_backend=backend)
    ) as sender:
        result = sender.send(case)
    assert result.outcome == "response" and result.request is not None
    assert result.request.content == (b"null" if has_body else None)
    raw = b"".join(backend.streams[0].writes)
    headers, body = raw.split(b"\r\n\r\n", 1)
    assert body == (b"null" if has_body else b"")
    assert (b"Content-Type: application/json" in headers) is has_body
    assert (b"Content-Length: 4" in headers) is has_body


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (httpcore.ConnectTimeout, httpx.ConnectTimeout),
        (httpcore.ReadTimeout, httpx.ReadTimeout),
        (httpcore.WriteTimeout, httpx.WriteTimeout),
        (httpcore.PoolTimeout, httpx.PoolTimeout),
        (httpcore.ConnectError, httpx.ConnectError),
        (httpcore.ReadError, httpx.ReadError),
        (httpcore.WriteError, httpx.WriteError),
        (httpcore.LocalProtocolError, httpx.LocalProtocolError),
        (httpcore.RemoteProtocolError, httpx.RemoteProtocolError),
        (httpcore.ProxyError, httpx.ProxyError),
        (httpcore.UnsupportedProtocol, httpx.UnsupportedProtocol),
        (httpcore.TimeoutException, httpx.TimeoutException),
        (httpcore.NetworkError, httpx.NetworkError),
        (httpcore.ProtocolError, httpx.ProtocolError),
    ],
)
def test_adapter_preserves_httpx_error_types(
    source: type[Exception],
    target: type[httpx.RequestError],
) -> None:
    class FailingBackend(MemoryBackend):
        def connect_tcp(
            self,
            host: str,
            port: int,
            timeout: float | None = None,
            local_address: str | None = None,
            socket_options: Iterable[SocketOption] | None = None,
        ) -> httpcore.NetworkStream:
            raise source("transport failure")

    with httpx.Client(transport=EOFTrackingTransport(network_backend=FailingBackend([]))) as client:
        with pytest.raises(target) as caught:
            client.get("http://api.example.test")
    assert type(caught.value) is target
    assert isinstance(caught.value.__cause__, source)
    assert caught.value.request.url.host == "api.example.test"
