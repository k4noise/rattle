"""Неизменяемые HTTP-снимки и результаты выполнения мутаций."""

from dataclasses import dataclass
from typing import Literal

from rattle.models import HttpMethod
from rattle.requests import MutationCase

SendOutcome = Literal["response", "crash", "unsendable", "serialization_error", "network_error"]
ErrorKind = Literal[
    "serialization", "unsendable", "timeout", "connection", "response", "network", "probe_limit"
]
Headers = tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class SerializedRequest:
    """Готовый запрос; content=None означает отсутствие тела, b'null' — JSON null."""

    method: HttpMethod
    url: str
    headers: Headers = ()
    content: bytes | None = None


@dataclass(frozen=True)
class HTTPResponseData:
    """Тело содержит сырые байты без распаковки Content-Encoding."""

    status_code: int
    headers: Headers = ()
    content: bytes = b""
    complete: bool = True
    truncated: bool = False


@dataclass(frozen=True)
class SendError:
    kind: ErrorKind
    message: str


@dataclass(frozen=True)
class AvailabilityResult:
    """available=True только при полном HTTP-ответе явно указанного probe URL."""

    url: str
    available: bool
    status_code: int | None = None
    error: SendError | None = None


@dataclass(frozen=True)
class SendResult:
    """case сохраняется по ссылке; HTTP-запрос и ответ — независимые байтовые снимки.

    request описывает попытку отправки, а не доказывает доставку серверу.
    При ошибке сериализации request=None; при обрыве response может быть частичным.
    """

    case: MutationCase
    outcome: SendOutcome
    request: SerializedRequest | None = None
    response: HTTPResponseData | None = None
    error: SendError | None = None
    availability: AvailabilityResult | None = None

    @property
    def is_crash(self) -> bool:
        return self.outcome == "crash"
