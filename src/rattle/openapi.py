"""Интерфейс парсера OpenAPI-схемы целевого API."""

from rattle.models import OpenAPISpec


class OpenAPIParser:
    """Контракт переиспользуемого парсера OpenAPI-документов."""

    def parse(self, raw_spec: str | bytes) -> OpenAPISpec:
        """Разбирает JSON/YAML-документ независимо от предыдущих вызовов."""
        raise NotImplementedError("OpenAPIParser.parse пока не реализован")
