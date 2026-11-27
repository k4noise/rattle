from copy import deepcopy
from typing import cast

import httpx
import pytest

from rattle.exceptions import RequestSerializationError, UnsendableRequestError
from rattle.models import ParamLocation
from rattle.requests import JSONValue, RequestData, RequestParameter
from rattle.serialization import RequestSerializer


@pytest.mark.parametrize("location", ["path", "query", "header", "cookie"])
@pytest.mark.parametrize("explode", [False, True])
@pytest.mark.parametrize(
    ("value", "simple", "form"),
    [
        (5, "5", "id=5"),
        ([3, 4, 5], "3,4,5", "id=3,4,5"),
        (
            {"role": "admin", "firstName": "Alex"},
            "role,admin,firstName,Alex",
            "id=role,admin,firstName,Alex",
        ),
    ],
)
def test_supported_style_matrix(
    location: ParamLocation, explode: bool, value: JSONValue, simple: str, form: str
) -> None:
    parameter = RequestParameter(
        "id", location, value, "simple" if location in {"path", "header"} else "form", explode
    )
    path = "/users/{id}" if location == "path" else "/users"
    request = RequestSerializer("https://api.example.test/v1/").serialize(
        RequestData("GET", path, (parameter,))
    )
    if explode and isinstance(value, dict):
        simple, form = "role=admin,firstName=Alex", "role=admin&firstName=Alex"
    if explode and isinstance(value, list):
        form = "id=3&id=4&id=5"
    if location == "path":
        assert request.url == "https://api.example.test/v1/users/" + simple
    elif location == "header":
        assert request.headers == (("id", simple),)
    elif location == "query":
        assert request.url == "https://api.example.test/v1/users?" + form
    else:
        assert request.headers == (("Cookie", form.replace("&", "; ")),)


@pytest.mark.parametrize(
    ("value", "encoded"),
    [(True, "true"), (False, "false"), (1.0, "1.0"), ("", ""), (2**100, str(2**100))],
)
def test_scalar_values_are_not_python_repr(value: JSONValue, encoded: str) -> None:
    request = RequestData("GET", "/", (RequestParameter("q", "query", value, "form", True),))
    assert (
        RequestSerializer("http://api.example.test")
        .serialize(request)
        .url.endswith("?q=" + encoded)
    )


def test_delimiters_in_components_are_encoded_but_style_delimiters_are_not() -> None:
    request = RequestData(
        "GET",
        "/{id}",
        (
            RequestParameter("id", "path", ["a/b", "x,y", "%2F"], "simple", True),
            RequestParameter("q name", "query", ["x,y", "a+b c", "я"], "form", False),
            RequestParameter("session", "cookie", "a; b=c", "form", True),
        ),
    )
    result = RequestSerializer("https://api.example.test/api").serialize(request)
    assert (
        result.url
        == "https://api.example.test/api/a%2Fb,x%2Cy,%252F?q%20name=x%2Cy,a%2Bb%20c,%D1%8F"
    )
    assert result.headers == (("Cookie", "session=a%3B%20b%3Dc"),)


def test_query_object_property_names_and_parameter_names_keep_encoding_and_order() -> None:
    request = RequestData(
        "GET",
        "/",
        (
            RequestParameter("obj", "query", {"a b": "c&d", "x": 1}, "form", True),
            RequestParameter("x", "query", [2, 3], "form", True),
        ),
    )
    assert (
        RequestSerializer("https://api.example.test")
        .serialize(request)
        .url.endswith("?a%20b=c%26d&x=1&x=2&x=3")
    )


def test_allow_reserved_is_used_for_query_values_only() -> None:
    request = RequestData(
        "GET",
        "/",
        (RequestParameter("q/", "query", "/a?b:c@d!$'()*,=[]", "form", True, True),),
    )
    result = RequestSerializer("https://api.example.test").serialize(request)
    assert result.url.endswith("?q%2F=/a?b:c@d!$'()*,=[]")
    assert httpx.URL(result.url).fragment == ""


@pytest.mark.parametrize("id_value", [".", ".."])
def test_path_dot_values_are_not_silently_normalized_to_another_endpoint(id_value: str) -> None:
    request = RequestData(
        "GET", "/items/{id}", (RequestParameter("id", "path", id_value, "simple", False),)
    )
    result = RequestSerializer("https://api.example.test/api").serialize(request)
    assert result.url == "https://api.example.test/api/items/" + "%2E" * len(id_value)
    assert httpx.URL(result.url).raw_path.endswith(("/items/" + "%2E" * len(id_value)).encode())


def test_absent_and_null_bodies_have_different_content_and_content_type() -> None:
    serializer = RequestSerializer("https://api.example.test")
    absent = serializer.serialize(RequestData("POST", "/"))
    null = serializer.serialize(RequestData("POST", "/", has_body=True))
    assert absent.content is None and absent.headers == ()
    assert null.content == b"null" and null.headers == (("Content-Type", "application/json"),)


def test_json_is_snapshotted_without_modifying_the_case_or_sharing_mutable_values() -> None:
    body: dict[str, JSONValue] = {"items": [True, 1, None, "я"]}
    data = RequestData(
        "POST",
        "/",
        body=body,
        has_body=True,
        content_type="application/problem+json; charset=utf-8",
    )
    before = deepcopy(data)
    result = RequestSerializer("https://api.example.test").serialize(data)
    assert data == before
    assert result.content == b'{"items":[true,1,null,"\\u044f"]}'
    assert result.headers == (("Content-Type", "application/problem+json; charset=utf-8"),)
    body["items"] = []
    assert result.content != b'{"items":[]}'


@pytest.mark.parametrize("value", [None, [None], {"x": None}, [[1]], {"x": [1]}])
@pytest.mark.parametrize("location", ["path", "query", "header", "cookie"])
def test_null_and_nested_parameter_values_are_explicitly_unsendable(
    value: JSONValue, location: ParamLocation
) -> None:
    request = RequestData(
        "GET",
        "/{q}" if location == "path" else "/",
        (
            RequestParameter(
                "q", location, value, "simple" if location in {"path", "header"} else "form", True
            ),
        ),
    )
    with pytest.raises(UnsendableRequestError):
        RequestSerializer("https://api.example.test").serialize(request)


@pytest.mark.parametrize("value", [[], {}])
@pytest.mark.parametrize("location", ["query", "cookie"])
def test_empty_exploded_collections_do_not_silently_disappear(
    value: JSONValue, location: ParamLocation
) -> None:
    request = RequestData("GET", "/", (RequestParameter("q", location, value, "form", True),))
    with pytest.raises(UnsendableRequestError):
        RequestSerializer("https://api.example.test").serialize(request)
    request = RequestData("GET", "/", (RequestParameter("q", location, value, "form", False),))
    result = RequestSerializer("https://api.example.test").serialize(request)
    if location == "query":
        assert result.url.endswith("?q=")
    else:
        assert result.headers == (("Cookie", "q="),)


@pytest.mark.parametrize(
    "data",
    [
        RequestData("GET", "/{id}"),
        RequestData("GET", "/", (RequestParameter("id", "path", 1, "simple", False),)),
        RequestData("GET", "/{id", (RequestParameter("id", "path", 1, "simple", False),)),
        RequestData("GET", "/", (RequestParameter("q", "query", 1, "deepObject", True),)),
        RequestData("GET", "/", (RequestParameter("q", "header", 1, "simple", False, True),)),
    ],
)
def test_unrepresentable_request_models_fail_before_io(data: RequestData) -> None:
    with pytest.raises(UnsendableRequestError):
        RequestSerializer("https://api.example.test").serialize(data)


@pytest.mark.parametrize(
    "name",
    [
        "Host",
        "Content-Length",
        "Transfer-Encoding",
        "Connection",
        "Content-Type",
        "Bad Header",
        "X\r\nHeader",
    ],
)
def test_controlled_or_invalid_header_names_are_not_sent(name: str) -> None:
    request = RequestData("GET", "/", (RequestParameter(name, "header", "x", "simple", False),))
    with pytest.raises(RequestSerializationError):
        RequestSerializer("https://api.example.test").serialize(request)


@pytest.mark.parametrize("value", ["\r\nX: injected", "\x00", "\x7f", " leading", "trailing ", "я"])
def test_invalid_or_unrepresentable_header_values_are_not_sent(value: str) -> None:
    request = RequestData(
        "GET", "/", (RequestParameter("X-Test", "header", value, "simple", False),)
    )
    with pytest.raises(RequestSerializationError):
        RequestSerializer("https://api.example.test").serialize(request)


def test_latin1_header_values_and_cookie_conflicts_are_handled_explicitly() -> None:
    parameter = RequestParameter("X-Test", "header", "café", "simple", False)
    result = RequestSerializer("https://api.example.test").serialize(
        RequestData("GET", "/", (parameter,))
    )
    assert result.headers == (("X-Test", "café"),)
    with pytest.raises(RequestSerializationError):
        RequestSerializer("https://api.example.test").serialize(
            RequestData(
                "GET",
                "/",
                (
                    RequestParameter("Cookie", "header", "manual=1", "simple", False),
                    RequestParameter("session", "cookie", 2, "form", True),
                ),
            )
        )


@pytest.mark.parametrize("path", ["/a?b", "/a#b", "/bad%XX", "/a/../b"])
def test_literal_paths_are_not_silently_reinterpreted(path: str) -> None:
    with pytest.raises(RequestSerializationError):
        RequestSerializer("https://api.example.test").serialize(RequestData("GET", path))


@pytest.mark.parametrize(
    "base_url",
    [
        "/api",
        "ftp://api.example.test",
        "https://user:pass@api.example.test",
        "https://api.example.test/?x=1",
        "https://api.example.test/#fragment",
        "https://",
        "http://api.example.test:bad",
    ],
)
def test_bad_base_urls_are_configuration_errors(base_url: str) -> None:
    with pytest.raises(ValueError):
        RequestSerializer(base_url)


@pytest.mark.parametrize("body", [float("nan"), float("inf"), {1: "key"}, (1, 2), b"bytes"])
def test_non_json_values_are_not_silently_converted(body: object) -> None:
    with pytest.raises(RequestSerializationError):
        RequestSerializer("https://api.example.test").serialize(
            RequestData("POST", "/", body=cast(JSONValue, body), has_body=True)
        )


def test_cycles_and_oversized_data_are_bounded() -> None:
    body: list[JSONValue] = []
    body.append(body)
    serializer = RequestSerializer("https://api.example.test")
    with pytest.raises(RequestSerializationError):
        serializer.serialize(RequestData("POST", "/", body=body, has_body=True))
    with pytest.raises(RequestSerializationError):
        RequestSerializer("https://api.example.test", max_request_bytes=100).serialize(
            RequestData("POST", "/", body="x" * 1000, has_body=True)
        )
    with pytest.raises(RequestSerializationError):
        RequestSerializer("https://api.example.test", max_depth=0).serialize(
            RequestData("POST", "/", body=[1], has_body=True)
        )


@pytest.mark.parametrize(
    "data",
    [
        RequestData("POST", "/", body="forgot has_body"),
        RequestData("POST", "/", content_type="application/json"),
        RequestData("POST", "/", has_body=True, content_type="text/plain"),
        RequestData("POST", "/", has_body=True, content_type=""),
        RequestData(
            "GET",
            "/",
            (
                RequestParameter("X-Q", "header", 1, "simple", False),
                RequestParameter("x-q", "header", 2, "simple", False),
            ),
        ),
    ],
)
def test_inconsistent_body_metadata_and_duplicate_headers_are_errors(data: RequestData) -> None:
    with pytest.raises(RequestSerializationError):
        RequestSerializer("https://api.example.test").serialize(data)


@pytest.mark.parametrize("suffix", ["/a/../base", "/bad%XX", "/a\\b", ":0", ":65536"])
def test_base_url_does_not_silently_normalize_to_another_target(suffix: str) -> None:
    with pytest.raises(ValueError):
        RequestSerializer("https://api.example.test" + suffix)


@pytest.mark.parametrize("prefix", ["", "/", "/api", "/api/", "/api//"])
def test_joining_keeps_the_base_path_prefix_and_endpoint_trailing_slash(prefix: str) -> None:
    url = RequestSerializer("https://api.example.test" + prefix).serialize(
        RequestData("GET", "/items/")
    )
    assert url.url == "https://api.example.test" + prefix.removesuffix("/") + "/items/"


def test_missing_path_error_identifies_the_missing_value() -> None:
    with pytest.raises(UnsendableRequestError, match="path-параметра: id"):
        RequestSerializer("https://api.example.test").serialize(RequestData("GET", "/items/{id}"))


@pytest.mark.parametrize("value", ["a\ud800b", "\udfff"])
def test_non_utf8_parameter_strings_have_an_explicit_serialization_error(value: str) -> None:
    data = RequestData("GET", "/", (RequestParameter("q", "query", value, "form", True),))
    with pytest.raises(RequestSerializationError):
        RequestSerializer("https://api.example.test").serialize(data)


def test_dictionary_keys_and_empty_strings_are_not_omitted() -> None:
    parameters = (
        RequestParameter("q", "query", {"empty": "", "a/b": "c,d"}, "form", False),
        RequestParameter("session", "cookie", "", "form", True),
        RequestParameter("X-Empty", "header", "", "simple", False),
    )
    result = RequestSerializer("https://api.example.test").serialize(
        RequestData("GET", "/", parameters)
    )
    assert result.url.endswith("?q=empty,,a%2Fb,c%2Cd")
    assert result.headers == (("X-Empty", ""), ("Cookie", "session="))
