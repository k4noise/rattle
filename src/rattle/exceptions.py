"""Собственные исключения пакета rattle."""


class RattleError(Exception):
    """Базовое исключение сервиса."""


class InvalidOpenAPISpecError(RattleError):
    """Ошибка разбора или проверки OpenAPI-схемы."""


class RequestGenerationError(RattleError):
    """Базовая ошибка построения запроса."""


class UnsupportedSchemaError(RequestGenerationError):
    """Генератор не поддерживает эту схему или настройки параметра."""


class NoExampleError(RequestGenerationError):
    """Не найден подходящий базовый пример в поддерживаемом подмножестве."""


class GenerationLimitError(RequestGenerationError):
    """Превышен заданный лимит работы или размера примера."""
