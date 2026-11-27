"""Детерминированная сериализация поддерживаемого подмножества simple/form."""

import json
import math
import re
from collections.abc import Callable
from urllib.parse import quote, urlsplit

import httpx

from rattle.exceptions import RequestSerializationError, UnsendableRequestError
from rattle.http_models import SerializedRequest
from rattle.requests import JSONValue, RequestData, RequestParameter

_STYLES = {"path": "simple", "query": "form", "header": "simple", "cookie": "form"}
_RESERVED = ":/?[]@!$&'()*+,;="
_AMBIGUOUS_QUERY = "#&+;"  # fragment, новая пара, form-пробел, альтернативный разделитель.
_TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
_PLACEHOLDER = re.compile(r"\{([^{}]+)\}")
_CONTROLLED_HEADERS = frozenset(
    {
        "host",
        "content-length",
        "transfer-encoding",
        "connection",
        "te",
        "trailer",
        "upgrade",
        "expect",
        "content-type",
    }
)


def http_url(value: str, *, allow_query: bool = False) -> httpx.URL:
    """Проверяет конфигурационный URL, не включая сеть и URL-авторизацию."""
    try:
        if not value or value != value.strip() or any(ord(char) < 32 for char in value):
            raise ValueError("URL содержит пробелы по краям или управляющие символы")
        parts = urlsplit(value)
        if "\\" in value or any(
            re.search(r"%(?![0-9a-fA-F]{2})", part) for part in (parts.path, parts.query)
        ):
            raise ValueError("URL содержит обратный слеш или некорректный percent-escape")
        if any(part in {".", ".."} for part in parts.path.split("/")):
            raise ValueError("Dot-сегменты URL привели бы к неявной смене пути")
        url = httpx.URL(value)
        if url.scheme not in {"http", "https"} or not url.host:
            raise ValueError("Нужен абсолютный HTTP(S) URL")
        if url.port is not None and not 1 <= url.port <= 65_535:
            raise ValueError("Порт должен быть в диапазоне 1..65535")
        if "@" in parts.netloc or "#" in value or not allow_query and "?" in value:
            raise ValueError("URL не должен содержать авторизацию, fragment или query базового URL")
        return url
    except (httpx.InvalidURL, UnicodeError) as error:
        raise ValueError(f"Некорректный HTTP URL: {error}") from error


def positive_int(name: str, value: int, *, allow_zero: bool = False) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < (0 if allow_zero else 1):
        raise ValueError(f"Недопустимое значение {name}")


class _JSONBudget:
    """Проверка JSON без копирования; ограничивает и вход до расширяющего кодирования."""

    def __init__(self, max_bytes: int, max_nodes: int, max_depth: int) -> None:
        self.max_bytes, self.max_nodes, self.max_depth = max_bytes, max_nodes, max_depth
        self.nodes = self.size = 0

    def check(self, value: object, depth: int = 0, active: frozenset[int] = frozenset()) -> None:
        self.nodes += 1
        if self.nodes > self.max_nodes or depth > self.max_depth:
            raise RequestSerializationError("Превышен бюджет или глубина сериализации")
        if value is None or isinstance(value, bool):
            self.size += 4 if value is None or value is True else 5
        elif isinstance(value, str):
            self.size += len(value) + 2
        elif isinstance(value, (int, float)):
            if isinstance(value, float) and not math.isfinite(value):
                raise RequestSerializationError("Неконечное число не является JSON-значением")
            self.size += len(json.dumps(value, allow_nan=False))
        elif isinstance(value, (list, dict)):
            if id(value) in active:
                raise RequestSerializationError("Цикл внутри значения запроса")
            self.size += 2 + len(value)
            self._check_size()
            active = active | {id(value)}
            if isinstance(value, list):
                for child in value:
                    self.check(child, depth + 1, active)
            else:
                for key, child in value.items():
                    if not isinstance(key, str):
                        raise RequestSerializationError("Ключи JSON-объекта должны быть строками")
                    self.check(key, depth + 1, active)
                    self.check(child, depth + 1, active)
        else:
            raise RequestSerializationError("Допускаются только JSON-значения")
        self._check_size()

    def _check_size(self) -> None:
        if self.size > self.max_bytes:
            raise RequestSerializationError("Превышен размер данных запроса")


def _scalar(value: JSONValue) -> str:
    if isinstance(value, str):
        return value
    if value is None or isinstance(value, (list, dict)):
        raise UnsendableRequestError(
            "Null и вложенные коллекции параметров не имеют поддерживаемого представления"
        )
    return json.dumps(value, allow_nan=False)


def _simple(parameter: RequestParameter, encode: Callable[[str], str]) -> str:
    value = parameter.value
    if isinstance(value, list):
        return ",".join(encode(_scalar(item)) for item in value)
    if isinstance(value, dict):
        if parameter.explode:
            return ",".join(
                encode(key) + "=" + encode(_scalar(item)) for key, item in value.items()
            )
        return ",".join(
            component
            for key, item in value.items()
            for component in (encode(key), encode(_scalar(item)))
        )
    return encode(_scalar(value))


def _form(parameter: RequestParameter) -> list[tuple[str, str]]:
    name = quote(parameter.name, safe="")
    value = parameter.value
    ambiguous = _AMBIGUOUS_QUERY
    if not parameter.explode and isinstance(value, (list, dict)):
        ambiguous += ","  # Запятая данных не должна становиться разделителем CSV.

    def encode(text: str) -> str:
        if parameter.allow_reserved:
            for separator in ambiguous:
                if separator in text:
                    raise UnsendableRequestError(
                        f"allowReserved оставляет неоднозначный разделитель {separator!r} в query"
                    )
        return quote(text, safe=_RESERVED if parameter.allow_reserved else "")

    if parameter.explode and isinstance(value, (list, dict)):
        if not value:
            raise UnsendableRequestError(
                "Пустая exploded-коллекция привела бы к молчаливому пропуску параметра"
            )
        if isinstance(value, list):
            return [(name, encode(_scalar(item))) for item in value]
        return [(quote(key, safe=""), encode(_scalar(item))) for key, item in value.items()]
    return [(name, _simple(parameter, encode))]


def _header(name: str, value: str) -> tuple[str, str]:
    if not _TOKEN.fullmatch(name):
        raise RequestSerializationError("Некорректное имя HTTP-заголовка")
    if value != value.strip(" \t") or any(
        ord(char) < 32 and char != "\t" or ord(char) == 127 for char in value
    ):
        raise RequestSerializationError("Недопустимые пробелы или управляющие символы в заголовке")
    try:
        value.encode("latin-1")
    except UnicodeEncodeError as error:
        raise UnsendableRequestError("Значение заголовка не представимо в Latin-1") from error
    return name, value


class RequestSerializer:
    """Без сетевых вызовов; один вызов не изменяет данные и не сохраняет их между запросами."""

    def __init__(
        self,
        base_url: str,
        *,
        max_request_bytes: int = 1_048_576,
        max_nodes: int = 100_000,
        max_depth: int = 64,
    ) -> None:
        self.base_url = http_url(base_url)
        positive_int("max_request_bytes", max_request_bytes)
        positive_int("max_nodes", max_nodes)
        positive_int("max_depth", max_depth, allow_zero=True)
        self.max_request_bytes, self.max_nodes, self.max_depth = (
            max_request_bytes,
            max_nodes,
            max_depth,
        )

    def serialize(self, request: RequestData) -> SerializedRequest:
        try:
            return self._serialize(request)
        except RequestSerializationError:
            raise
        except (
            ValueError,
            TypeError,
            UnicodeError,
            RecursionError,
            OverflowError,
            httpx.InvalidURL,
        ) as error:
            raise RequestSerializationError(f"Не удалось сериализовать запрос: {error}") from error

    def _serialize(self, request: RequestData) -> SerializedRequest:
        if request.method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            raise RequestSerializationError("Неподдерживаемый HTTP-метод")
        path = request.path
        if not path.startswith("/") or "?" in path or "#" in path:
            raise RequestSerializationError("Нужен шаблон абсолютного пути без query и fragment")
        if re.search(r"%(?![0-9a-fA-F]{2})", path) or any(
            part in {".", ".."} for part in path.split("/")
        ):
            raise RequestSerializationError("Некорректные percent-escapes или dot-сегменты в пути")
        budget = _JSONBudget(self.max_request_bytes, self.max_nodes, self.max_depth)
        budget.check(path)
        by_path: dict[str, str] = {}
        headers: list[tuple[str, str]] = []
        query: list[tuple[str, str]] = []
        cookies: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for parameter in request.parameters:
            budget.check(parameter.name)
            budget.check(parameter.value)
            if not parameter.name or _STYLES.get(parameter.location) != parameter.style:
                raise UnsendableRequestError(
                    "Неподдерживаемое имя, расположение или style параметра"
                )
            if not isinstance(parameter.explode, bool) or not isinstance(
                parameter.allow_reserved, bool
            ):
                raise RequestSerializationError("explode и allowReserved должны быть булевыми")
            if parameter.allow_reserved and parameter.location != "query":
                raise UnsendableRequestError("allowReserved=true поддерживается только для query")
            key = (
                parameter.location,
                parameter.name.lower() if parameter.location == "header" else parameter.name,
            )
            if key in seen:
                raise RequestSerializationError("Повторяющийся параметр запроса")
            seen.add(key)
            if parameter.location == "path":
                by_path[parameter.name] = _simple(parameter, lambda text: quote(text, safe=""))
            elif parameter.location == "query":
                query.extend(_form(parameter))
            elif parameter.location == "cookie":
                cookies.extend(_form(parameter))
            else:
                if parameter.name.lower() in _CONTROLLED_HEADERS:
                    raise UnsendableRequestError("Заголовок управляется HTTP-отправителем")
                headers.append(_header(parameter.name, _simple(parameter, lambda text: text)))
        expected = {match.group(1) for match in _PLACEHOLDER.finditer(path)}
        if missing := expected - by_path.keys():
            raise UnsendableRequestError(
                "Нет значения path-параметра: " + ", ".join(sorted(missing))
            )
        if extra := by_path.keys() - expected:
            raise UnsendableRequestError("Лишний path-параметр: " + ", ".join(sorted(extra)))
        literal_parts = _PLACEHOLDER.split(path)
        encoded_path = "".join(
            by_path[part] if index % 2 else quote(part, safe="/%:@!$&'()*+,;=-._~")
            for index, part in enumerate(literal_parts)
        )
        if any(char in part for part in literal_parts[::2] for char in "{}"):
            raise UnsendableRequestError("Некорректные скобки в шаблоне пути")
        encoded_path = "/".join(
            "%2E" * len(part) if part in {".", ".."} else part for part in encoded_path.split("/")
        )
        raw_path = self.base_url.raw_path.removesuffix(b"/") + encoded_path.encode("ascii")
        if query:
            raw_path += b"?" + "&".join(name + "=" + value for name, value in query).encode("ascii")
        url = self.base_url.copy_with(raw_path=raw_path)
        if url.raw_path != raw_path:
            raise UnsendableRequestError("HTTP-клиент изменяет представление пути или query")
        if cookies:
            if any(name.lower() == "cookie" for name, _ in headers):
                raise RequestSerializationError("Cookie-заголовок конфликтует с cookie-параметрами")
            headers.append(
                _header("Cookie", "; ".join(name + "=" + value for name, value in cookies))
            )
        if not isinstance(request.has_body, bool):
            raise RequestSerializationError("has_body должен быть булевым")
        content = None
        if request.has_body:
            budget.check(request.body)
            media = "application/json" if request.content_type is None else request.content_type
            main_type = media.split(";", 1)[0].strip().lower()
            if main_type != "application/json" and not (
                main_type.startswith("application/") and main_type.endswith("+json")
            ):
                raise UnsendableRequestError("Поддерживается только JSON-тело")
            headers.append(_header("Content-Type", media))
            content = json.dumps(
                request.body, ensure_ascii=True, allow_nan=False, separators=(",", ":")
            ).encode("ascii")
        elif request.body is not None or request.content_type is not None:
            raise RequestSerializationError("Метаданные тела противоречат has_body=False")
        size = (
            len(str(url).encode("ascii"))
            + sum(len(name) + len(value.encode("latin-1")) + 4 for name, value in headers)
            + len(content or b"")
        )
        if size > self.max_request_bytes:
            raise RequestSerializationError("Превышен размер сериализованного запроса")
        return SerializedRequest(request.method, str(url), tuple(headers), content)
