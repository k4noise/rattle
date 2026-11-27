"""Последовательное HTTP-выполнение мутаций с явной проверкой доступности."""

import math
from collections.abc import Iterable, Iterator
from contextlib import ExitStack
from types import TracebackType
from typing import Self

import httpx

from rattle._http_transport import EOFTrackingTransport, peer_eof
from rattle.exceptions import (
    RequestSerializationError,
    SenderStateError,
    TargetUnavailableError,
    UnsendableRequestError,
)
from rattle.http_models import (
    AvailabilityResult,
    ErrorKind,
    Headers,
    HTTPResponseData,
    SendError,
    SendResult,
    SerializedRequest,
)
from rattle.requests import MutationCase
from rattle.serialization import RequestSerializer, http_url, positive_int


def _headers(headers: httpx.Headers) -> Headers:
    return tuple((name.decode("latin-1"), value.decode("latin-1")) for name, value in headers.raw)


def _error(error: httpx.RequestError) -> SendError:
    kind: ErrorKind
    if isinstance(error, httpx.TimeoutException):
        kind = "timeout"
    elif isinstance(error, httpx.ConnectError):
        kind = "connection"
    elif isinstance(error, httpx.RemoteProtocolError):
        kind = "protocol"
    elif isinstance(error, httpx.ReadError):
        kind = "response"
    else:
        kind = "network"
    return SendError(kind, str(error) or type(error).__name__)


def _exchange(
    client: httpx.Client,
    request: httpx.Request,
    limit: int,
) -> tuple[HTTPResponseData | None, httpx.RequestError | None]:
    response = None
    error = None
    content = bytearray()
    complete = truncated = False
    try:
        response = client.send(request, stream=True, follow_redirects=False, auth=None)
        # MockTransport может вернуть уже загруженный Response. Реальная сеть читается raw,
        # без распаковки Content-Encoding и без неограниченного response.read().
        chunks = (response.content,) if response.is_stream_consumed else response.iter_raw()
        for chunk in chunks:
            remaining = limit - len(content)
            content.extend(chunk[:remaining])
            if len(chunk) > remaining:
                truncated = True
                break
        else:
            complete = True
    except httpx.RequestError as caught:
        error = caught
    finally:
        if response is not None:
            try:
                response.close()
            except httpx.RequestError as caught:
                if error is None:
                    error = caught
        # Cookie jar не участвует в построении запросов и не должен расти между кейсами.
        client.cookies.clear()
    snapshot = (
        None
        if response is None
        else HTTPResponseData(
            response.status_code,
            _headers(response.headers),
            bytes(content),
            complete,
            truncated,
            peer_eof=peer_eof(response),
        )
    )
    return snapshot, error


def _positive_timeout(name: str, value: float) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{name} должен быть конечным положительным числом")


def _origin(url: httpx.URL) -> tuple[str, str, int]:
    port = (443 if url.scheme == "https" else 80) if url.port is None else url.port
    return url.scheme, url.host, port


class HTTPSender:
    """Используется через with; клиент и переданный transport принадлежат этому контексту.

    Не потокобезопасен. Пробы выполняются только по явно переданному availability_url.
    Отправка не повторяется автоматически; redirects, ambient proxies и cookie state отключены.
    """

    def __init__(
        self,
        base_url: str,
        *,
        availability_url: str | None = None,
        timeout: float = 5.0,
        availability_timeout: float = 2.0,
        max_unavailable: int = 3,
        max_response_bytes: int = 65_536,
        max_request_bytes: int = 1_048_576,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._serializer = RequestSerializer(base_url, max_request_bytes=max_request_bytes)
        self._availability_url = (
            None if availability_url is None else http_url(availability_url, allow_query=True)
        )
        if self._availability_url is not None and _origin(self._availability_url) != _origin(
            self._serializer.base_url
        ):
            raise ValueError("availability_url должен иметь тот же origin, что и базовый URL")
        _positive_timeout("timeout", timeout)
        _positive_timeout("availability_timeout", availability_timeout)
        positive_int("max_unavailable", max_unavailable)
        positive_int("max_response_bytes", max_response_bytes)
        self._timeout, self._availability_timeout = timeout, availability_timeout
        self._max_unavailable, self._response_limit = max_unavailable, max_response_bytes
        self._transport = transport
        self._client: httpx.Client | None = None
        self._stack: ExitStack | None = None
        self._closed = self._aborted = False
        self._failures = 0

    def __enter__(self) -> Self:
        if self._client is not None or self._closed:
            raise SenderStateError("HTTPSender нельзя открыть повторно")
        client = httpx.Client(
            timeout=self._timeout,
            transport=EOFTrackingTransport() if self._transport is None else self._transport,
            trust_env=False,
            follow_redirects=False,
            auth=None,
        )
        stack = ExitStack()
        try:
            self._client = stack.enter_context(client)
        except BaseException:
            self._closed = True
            client.close()
            raise
        self._stack = stack
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._closed = True
        self._client = None
        if self._stack is not None:
            self._stack.__exit__(exc_type, exc_value, traceback)

    def _active_client(self) -> httpx.Client:
        if self._client is None or self._closed or self._aborted:
            raise SenderStateError("Нужен активный with HTTPSender без остановки по сетевой ошибке")
        return self._client

    def send(self, case: MutationCase) -> SendResult:
        """Возвращает результат; после порога сетевых сбоев поднимает TargetUnavailableError.

        Ошибки данных не отправляются, не считаются падением и не меняют счётчик сбоев.
        Ошибки программирования не маскируются под результаты проверки API.
        """
        client = self._active_client()
        try:
            serialized = self._serializer.serialize(case.request)
            # Request создаётся напрямую: build_request() добавлял бы cookies клиента.
            request = httpx.Request(
                serialized.method,
                serialized.url,
                headers=[
                    (name.encode("ascii"), value.encode("latin-1"))
                    for name, value in serialized.headers
                ],
                content=serialized.content,
            )
            wire = SerializedRequest(
                serialized.method, str(request.url), _headers(request.headers), serialized.content
            )
            size = (
                len(wire.url.encode("ascii"))
                + sum(len(name) + len(value.encode("latin-1")) + 4 for name, value in wire.headers)
                + len(wire.content or b"")
            )
            if size > self._serializer.max_request_bytes:
                raise RequestSerializationError(
                    "Превышен размер запроса с транспортными заголовками"
                )
        except UnsendableRequestError as error:
            return SendResult(case, "unsendable", error=SendError("unsendable", str(error)))
        except (RequestSerializationError, ValueError, UnicodeError, httpx.InvalidURL) as error:
            return SendResult(
                case, "serialization_error", error=SendError("serialization", str(error))
            )
        response, failure = _exchange(client, request, self._response_limit)
        error_info = None if failure is None else _error(failure)
        local_failure = isinstance(failure, (httpx.LocalProtocolError, httpx.UnsupportedProtocol))
        if local_failure:
            error_info = SendError("serialization", str(failure))
        if response is not None and 500 <= response.status_code <= 599:
            self._failures = 0
            return SendResult(case, "crash", wire, response, error_info)
        if local_failure:
            return SendResult(case, "serialization_error", wire, response, error_info)
        if failure is None:
            self._failures = 0
            return SendResult(case, "response", wire, response)
        # При транспортной ошибке доступность подтверждает только отдельный явный GET,
        # а не сам мутированный запрос или его повторная отправка.
        availability = None if self._availability_url is None else self._probe(client)
        confirmed = availability is not None and availability.available
        # RemoteProtocolError сам по себе не доказывает обрыв. Нужен наблюдаемый EOF
        # на соединении данного ответа, а не текст исключения или один Content-Length.
        interrupted = (
            response is not None
            and not response.complete
            and not response.truncated
            and (
                isinstance(failure, httpx.ReadError)
                or isinstance(failure, httpx.RemoteProtocolError)
                and response.peer_eof
            )
        )
        result = SendResult(
            case,
            "crash" if interrupted and confirmed else "network_error",
            wire,
            response,
            error_info,
            availability,
        )
        self._failures = 0 if confirmed else self._failures + 1
        if self._failures >= self._max_unavailable:
            self._aborted = True
            raise TargetUnavailableError(result, self._failures)
        return result

    def _probe(self, client: httpx.Client) -> AvailabilityResult:
        assert self._availability_url is not None
        request = httpx.Request(
            "GET",
            self._availability_url,
            extensions={"timeout": httpx.Timeout(self._availability_timeout).as_dict()},
        )
        response, failure = _exchange(client, request, self._response_limit)
        status = None if response is None else response.status_code
        if failure is not None:
            return AvailabilityResult(str(self._availability_url), False, status, _error(failure))
        if response is None or not response.complete:
            return AvailabilityResult(
                str(self._availability_url),
                False,
                status,
                SendError(
                    "probe_limit", "Не получен полный ограниченный ответ проверки доступности"
                ),
            )
        return AvailabilityResult(str(self._availability_url), True, status)

    def send_all(self, cases: Iterable[MutationCase]) -> Iterator[SendResult]:
        """Ленивый последовательный обход, без извлечения следующего кейса после остановки."""
        self._active_client()
        iterator = iter(cases)
        while True:
            self._active_client()
            try:
                case = next(iterator)
            except StopIteration:
                return
            yield self.send(case)
