"""Собственные исключения пакета rattle."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rattle.http_models import SendResult


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


class RequestSendingError(RattleError):
    """Базовая ошибка подготовки и выполнения HTTP-запросов."""


class RequestSerializationError(RequestSendingError):
    """Данные запроса нельзя корректно сериализовать."""


class UnsendableRequestError(RequestSerializationError):
    """Вариант не имеет поддерживаемого HTTP-представления."""


class SenderStateError(RequestSendingError):
    """Отправитель используется вне своего контекстного менеджера или после остановки."""


class TargetUnavailableError(RequestSendingError):
    """Серия прервана; последний результат сохранён, следующий вариант не извлекается."""

    def __init__(self, last_result: "SendResult", consecutive_failures: int) -> None:
        self.last_result = last_result
        self.consecutive_failures = consecutive_failures
        super().__init__(
            f"Проверка остановлена после {consecutive_failures} сетевых сбоев "
            "без подтверждения доступности"
        )
