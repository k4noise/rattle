import pytest

from rattle.openapi import OpenAPIParser


@pytest.fixture
def parser() -> OpenAPIParser:
    return OpenAPIParser()
