"""Собственные исключения пакета rattle."""


class RattleError(Exception):
    """Базовое исключение сервиса."""


class InvalidOpenAPISpecError(RattleError):
    """Ошибка разбора или проверки OpenAPI-схемы."""
