"""Настоящий HTTPX/httpcore/h11 и локальный TCP-сервер, без внешних сервисов."""

import socketserver
from collections.abc import Iterator
from contextlib import contextmanager
from threading import Thread

import pytest

from rattle.exceptions import TargetUnavailableError
from rattle.requests import MutationCase, RequestData
from rattle.sending import HTTPSender

_READY = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"
_FIXED_EOF = b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\nConnection: close\r\n\r\nabc"
_CHUNKED = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n"


def _case() -> MutationCase:
    return MutationCase(RequestData("GET", "/items"), "wrong_type", "query", ("q",))


@contextmanager
def _api(response: bytes, probe: bytes = _READY) -> Iterator[tuple[str, list[bytes]]]:
    calls: list[bytes] = []

    class Handler(socketserver.StreamRequestHandler):
        def handle(self) -> None:
            self.request.settimeout(2)
            line = self.rfile.readline(8192)
            if not line:
                return
            while self.rfile.readline(8192) not in {b"\r\n", b"\n", b""}:
                pass
            calls.append(line.split(b" HTTP/", 1)[0])
            # Возврат из handle закрывает TCP: EOF создаёт сам стек HTTP, не MockTransport.
            self.wfile.write(probe if line.startswith(b"GET /ready ") else response)

    with socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler) as server:
        server.daemon_threads = True
        thread = Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_address[1]}", calls
        finally:
            server.shutdown()
            thread.join(timeout=3)
            assert not thread.is_alive()


@pytest.mark.parametrize(
    "wire",
    [
        _FIXED_EOF,
        _CHUNKED + b"A\r\nabc",  # не дочитан chunk размером 10 байт
        _CHUNKED + b"3\r\nabc\r\n",  # нет завершающего нулевого chunk
        _CHUNKED + b"3\r\nabc\r\n0\r\nX-Trailer: partial",  # оборваны trailers
    ],
)
def test_real_http_body_eof_with_live_probe_is_a_crash(wire: bytes) -> None:
    with _api(wire) as (base, calls):
        with HTTPSender(base, availability_url=base + "/ready", timeout=1) as sender:
            result = sender.send(_case())
    assert result.outcome == "crash" and result.is_crash
    assert result.error is not None and result.error.kind == "protocol"
    assert result.response is not None and result.response.status_code == 200
    assert result.response.content == b"abc" and not result.response.complete
    assert result.response.peer_eof is True and result.response.truncated is False
    assert result.availability is not None and result.availability.available is True
    assert calls == [b"GET /items", b"GET /ready"]


@pytest.mark.parametrize(
    "wire",
    [
        b"NOT HTTP\r\n\r\n",
        _CHUNKED + b"XYZ\r\n",  # невалидный размер chunk, а не EOF во время ожидания данных
        _CHUNKED + b"3\r\nabc\r\nXYZ\r\n",
    ],
)
def test_real_malformed_protocol_is_not_a_crash_even_when_probe_is_live(wire: bytes) -> None:
    with _api(wire) as (base, calls):
        with HTTPSender(base, availability_url=base + "/ready", timeout=1) as sender:
            result = sender.send(_case())
    assert result.outcome == "network_error" and not result.is_crash
    assert result.error is not None and result.error.kind == "protocol"
    if wire.startswith(b"NOT HTTP"):
        assert result.response is None
    else:
        assert result.response is not None and result.response.status_code == 200
        assert result.response.peer_eof is False
        assert result.response.complete is False
        assert result.response.content == (b"abc" if b"3\r\nabc" in wire else b"")
    assert result.availability is not None and result.availability.available is True
    assert calls == [b"GET /items", b"GET /ready"]


def test_real_body_eof_without_probe_stays_an_unconfirmed_network_error() -> None:
    with _api(_FIXED_EOF) as (base, calls):
        with HTTPSender(base, timeout=1) as sender:
            result = sender.send(_case())
    assert result.outcome == "network_error" and not result.is_crash
    assert result.response is not None and result.response.peer_eof is True
    assert result.response.content == b"abc"
    assert result.availability is None
    assert calls == [b"GET /items"]


def test_real_body_eof_with_failed_probe_stops_with_the_original_result() -> None:
    with _api(_FIXED_EOF, probe=b"") as (base, calls):
        with HTTPSender(
            base, availability_url=base + "/ready", max_unavailable=1, timeout=1
        ) as sender:
            with pytest.raises(TargetUnavailableError) as caught:
                sender.send(_case())
    result = caught.value.last_result
    assert result.outcome == "network_error" and not result.is_crash
    assert result.response is not None and result.response.peer_eof is True
    assert result.response.status_code == 200 and result.response.content == b"abc"
    assert result.availability is not None and not result.availability.available
    assert calls == [b"GET /items", b"GET /ready"]


def test_real_close_delimited_body_ends_normally_at_eof() -> None:
    wire = b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\nabc"
    with _api(wire) as (base, calls):
        with HTTPSender(base, availability_url=base + "/ready", timeout=1) as sender:
            result = sender.send(_case())
    assert result.outcome == "response" and result.error is None
    assert result.response is not None and result.response.complete is True
    assert result.response.content == b"abc" and result.response.peer_eof is True
    assert result.availability is None and calls == [b"GET /items"]


def test_local_response_cap_is_not_a_remote_eof_or_an_api_crash() -> None:
    wire = b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\nConnection: close\r\n\r\nabcdef"
    with _api(wire) as (base, calls):
        with HTTPSender(
            base, availability_url=base + "/ready", max_response_bytes=3, timeout=1
        ) as sender:
            result = sender.send(_case())
    assert result.outcome == "response" and result.error is None
    assert result.response is not None and result.response.truncated is True
    assert result.response.content == b"abc" and result.response.peer_eof is False
    assert result.availability is None and calls == [b"GET /items"]


@pytest.mark.parametrize("wire", [_FIXED_EOF, _CHUNKED + b"XYZ\r\n"])
def test_real_5xx_is_a_crash_without_needing_an_eof_or_a_probe(wire: bytes) -> None:
    with _api(wire.replace(b"200 OK", b"500 Error"), probe=b"") as (base, calls):
        with HTTPSender(
            base, availability_url=base + "/ready", max_unavailable=1, timeout=1
        ) as sender:
            result = sender.send(_case())
    assert result.is_crash and result.error is not None and result.error.kind == "protocol"
    assert result.response is not None and result.response.status_code == 500
    assert result.response.peer_eof is (wire == _FIXED_EOF)
    assert result.availability is None and calls == [b"GET /items"]
