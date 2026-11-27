"""HTTPX-адаптер с фактическим EOF, наблюдаемым через публичный backend HTTPcore.

RemoteProtocolError не содержит структурированной причины. Не угадываем её по тексту:
EOF фиксируется только при read(n > 0) == b"" на соединении данного ответа.
Ошибки синтаксиса HTTP, close() клиента и EOF другого соединения этот флаг не ставят.
"""

import ssl
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from types import TracebackType
from typing import Self, TypeAlias, cast

import httpcore
import httpx

SocketOption: TypeAlias = (
    tuple[int, int, int] | tuple[int, int, bytes | bytearray] | tuple[int, int, None, int]
)
_EOF_EXTENSION = "rattle.peer_eof"
_ERROR_TYPES: tuple[tuple[type[Exception], type[httpx.RequestError]], ...] = (
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
)


@contextmanager
def _map_errors() -> Iterator[None]:
    try:
        yield
    except Exception as error:
        for source, target in _ERROR_TYPES:
            if isinstance(error, source):
                raise target(str(error)) from error
        raise


@dataclass
class _EOFState:
    received: bool = False


@dataclass
class _RequestEpoch:
    generation: int = 0


class _EOFStream(httpcore.NetworkStream):
    def __init__(self, stream: httpcore.NetworkStream, epoch: _RequestEpoch) -> None:
        self._stream = stream
        self._epoch = epoch
        self._generation = epoch.generation
        self._state = _EOFState()

    @property
    def state(self) -> _EOFState:
        # Keep-alive может переиспользовать поток. Старые ответы сохраняют свой объект
        # состояния, даже если следующий запрос оборвался ещё до получения заголовков.
        if self._generation != self._epoch.generation:
            self._generation = self._epoch.generation
            self._state = _EOFState()
        return self._state

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        state = self.state
        data = self._stream.read(max_bytes, timeout=timeout)
        if max_bytes > 0 and data == b"":
            state.received = True
        return data

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self._stream.write(buffer, timeout=timeout)

    def close(self) -> None:
        self._stream.close()

    def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.NetworkStream:
        stream = self._stream.start_tls(
            ssl_context, server_hostname=server_hostname, timeout=timeout
        )
        return _EOFStream(stream, self._epoch)

    def get_extra_info(self, info: str) -> object:
        return cast(object, self._stream.get_extra_info(info))


class _EOFBackend(httpcore.NetworkBackend):
    def __init__(self, backend: httpcore.NetworkBackend) -> None:
        self._backend = backend
        self._epoch = _RequestEpoch()

    def begin_request(self) -> None:
        self._epoch.generation += 1

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[SocketOption] | None = None,
    ) -> httpcore.NetworkStream:
        return _EOFStream(
            self._backend.connect_tcp(
                host,
                port,
                timeout=timeout,
                local_address=local_address,
                socket_options=socket_options,
            ),
            self._epoch,
        )

    def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[SocketOption] | None = None,
    ) -> httpcore.NetworkStream:
        return _EOFStream(
            self._backend.connect_unix_socket(
                path,
                timeout=timeout,
                socket_options=socket_options,
            ),
            self._epoch,
        )

    def sleep(self, seconds: float) -> None:
        self._backend.sleep(seconds)


class _ResponseStream(httpx.SyncByteStream):
    def __init__(self, response: httpcore.Response) -> None:
        self._response = response

    def __iter__(self) -> Iterator[bytes]:
        with _map_errors():
            yield from self._response.iter_stream()

    def close(self) -> None:
        with _map_errors():
            self._response.close()


class EOFTrackingTransport(httpx.BaseTransport):
    """Синхронный HTTP/1.1, без proxy/retries и с обычной TLS-проверкой HTTPX.

    Использует только публичные API HTTPX/httpcore, без подмены HTTPTransport._pool.
    network_backend нужен для тестов с настоящим HTTP-парсером, но без TCP/TLS-сервера.
    Как и HTTPSender, рассчитан на последовательное, не конкурентное использование.
    """

    def __init__(self, *, network_backend: httpcore.NetworkBackend | None = None) -> None:
        backend = httpcore.SyncBackend() if network_backend is None else network_backend
        self._backend = _EOFBackend(backend)
        self._pool = httpcore.ConnectionPool(
            ssl_context=httpx.create_ssl_context(trust_env=False),
            network_backend=self._backend,
            http1=True,
            http2=False,
            retries=0,
            max_connections=1,
            max_keepalive_connections=1,
        )

    def __enter__(self) -> Self:
        self._pool.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None = None,
        exc_value: BaseException | None = None,
        traceback: TracebackType | None = None,
    ) -> None:
        with _map_errors():
            self._pool.__exit__(exc_type, exc_value, traceback)

    def close(self) -> None:
        with _map_errors():
            self._pool.close()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if not isinstance(request.stream, httpx.SyncByteStream):
            raise TypeError("Нужен синхронный поток HTTP-запроса")
        core_request = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=request.stream,
            extensions=request.extensions,
        )
        self._backend.begin_request()
        with _map_errors():
            response = self._pool.handle_request(core_request)
        extensions = dict(response.extensions)
        stream = extensions.get("network_stream")
        if isinstance(stream, _EOFStream):
            # Состояние обновится при последующем чтении тела, а не только заголовков.
            extensions[_EOF_EXTENSION] = stream.state
        return httpx.Response(
            response.status,
            headers=response.headers,
            stream=_ResponseStream(response),
            extensions=extensions,
        )


def peer_eof(response: httpx.Response) -> bool:
    state = response.extensions.get(_EOF_EXTENSION)
    return isinstance(state, _EOFState) and state.received
