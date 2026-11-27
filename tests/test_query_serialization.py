"""Query должен сохранять имена, значения и число пар после form-разбора."""

from urllib.parse import parse_qsl, urlsplit

import pytest

from rattle.exceptions import UnsendableRequestError
from rattle.requests import JSONValue, RequestData, RequestParameter
from rattle.serialization import RequestSerializer

BASE = "https://api.example.test"


def _query(parameter: RequestParameter) -> str:
    request = RequestData("GET", "/items", (parameter,))
    return urlsplit(RequestSerializer(BASE).serialize(request).url).query


@pytest.mark.parametrize("value", ["a&admin=true", "a+b", "a;b=c", "a#fragment"])
@pytest.mark.parametrize("explode", [False, True])
def test_allow_reserved_rejects_ambiguous_query_components(value: str, explode: bool) -> None:
    # Даже внутри массива/объекта эти символы не должны добавлять пару или менять значение.
    values: tuple[JSONValue, ...] = (value, [value, "safe"], {"key": value})
    for data in values:
        with pytest.raises(UnsendableRequestError, match="allowReserved"):
            _query(RequestParameter("q", "query", data, "form", explode, True))


@pytest.mark.parametrize("key", ["a&admin", "a+b", "a;b", "a#b", "a,b"])
def test_non_exploded_object_keys_cannot_inject_separators(key: str) -> None:
    with pytest.raises(UnsendableRequestError, match="allowReserved"):
        _query(RequestParameter("q", "query", {key: "safe"}, "form", False, True))


@pytest.mark.parametrize("value", [["one,two", "three"], {"key": "one,two"}])
def test_allow_reserved_rejects_data_commas_in_csv_collections(value: JSONValue) -> None:
    with pytest.raises(UnsendableRequestError, match="allowReserved"):
        _query(RequestParameter("q", "query", value, "form", False, True))


@pytest.mark.parametrize("value", ["a&admin=true", "a+b", "a;b=c", "a#fragment"])
def test_without_allow_reserved_scalar_round_trips_through_query_parsers(value: str) -> None:
    # explode не влияет на представление одиночного значения.
    raw = _query(RequestParameter("q&+;#", "query", value, "form", True))
    expected = [("q&+;#", value)]
    assert parse_qsl(raw, keep_blank_values=True, strict_parsing=True) == expected
    # Старые/настраиваемые form-парсеры могут считать ; альтернативой &.
    assert parse_qsl(raw.replace(";", "&"), keep_blank_values=True) == expected


@pytest.mark.parametrize("allow_reserved", [False, True])
def test_scalar_reserved_characters_do_not_change_the_decoded_query(allow_reserved: bool) -> None:
    value = "/a?b:c@d!$'()*,=[]~ я"
    parameters = (
        RequestParameter("q/", "query", value, "form", True, allow_reserved),
        RequestParameter("empty", "query", "", "form", True),
    )
    request = RequestSerializer(BASE).serialize(RequestData("GET", "/items", parameters))
    assert urlsplit(request.url).fragment == ""
    assert parse_qsl(urlsplit(request.url).query, keep_blank_values=True) == [
        ("q/", value),
        ("empty", ""),
    ]
    if allow_reserved:
        assert "/a?b:c@d!$'()*,=[]~" in request.url


@pytest.mark.parametrize("allow_reserved", [False, True])
def test_exploded_arrays_keep_repeated_names_values_and_commas(allow_reserved: bool) -> None:
    raw = _query(
        RequestParameter("q", "query", ["one,two", "/three?x=y", ""], "form", True, allow_reserved)
    )
    assert parse_qsl(raw, keep_blank_values=True) == [
        ("q", "one,two"),
        ("q", "/three?x=y"),
        ("q", ""),
    ]


@pytest.mark.parametrize("allow_reserved", [False, True])
def test_exploded_objects_encode_keys_independently_of_allow_reserved(allow_reserved: bool) -> None:
    raw = _query(
        RequestParameter(
            "q", "query", {"key&+;#,/": "/one?x=y", "empty": ""}, "form", True, allow_reserved
        )
    )
    expected = [("key&+;#,/", "/one?x=y"), ("empty", "")]
    assert parse_qsl(raw, keep_blank_values=True) == expected
    assert parse_qsl(raw.replace(";", "&"), keep_blank_values=True) == expected


@pytest.mark.parametrize("value", [["one", "/two?x=y"], {"key": "/one?x=y", "other": "two"}])
def test_unambiguous_csv_collections_still_have_the_expected_pairs(value: JSONValue) -> None:
    raw = _query(RequestParameter("q", "query", value, "form", False, True))
    expected = "one,/two?x=y" if isinstance(value, list) else "key,/one?x=y,other,two"
    assert parse_qsl(raw, keep_blank_values=True) == [("q", expected)]
