"""Диагностика: предупреждения, ограниченные по частоте.

Пустое окно камеры — частая жалоба, а причины были видны только в
debug-журнале. Теперь каждая неудача снимка/потока попадает в журнал
HA как WARNING, но не чаще одного раза за интервал (чтобы не заспамить
журнал при опросе раз в секунду).
"""
from __future__ import annotations

import logging
import time

_LAST_WARN: dict[str, float] = {}

#: стандартный интервал повторных предупреждений (секунд)
WARN_INTERVAL = 300.0


def safe_exc_text(err: BaseException) -> str:
    """Безопасное описание исключения для журнала: тип и HTTP-статус.

    Строковое представление исключений aiohttp (WSServerHandshakeError,
    ClientResponseError) содержит полный URL запроса, а в пути WSS-URL
    находится токен видеосессии — поэтому в журнал идёт только тип
    исключения и, если он есть, код HTTP-статуса (ревью HACS).
    """
    name = type(err).__name__
    status = getattr(err, "status", None)
    if isinstance(status, int) and status > 0:
        return f"{name} (HTTP {status})"
    return name


def warn_limited(
    logger: logging.Logger,
    key: str,
    msg: str,
    *args: object,
    interval: float = WARN_INTERVAL,
) -> None:
    """WARNING-сообщение не чаще одного раза за interval секунд на key."""
    now = time.monotonic()
    if now - _LAST_WARN.get(key, 0.0) >= interval:
        _LAST_WARN[key] = now
        logger.warning(msg, *args)


def reset_limited() -> None:
    """Сбросить ограничения (для тестов)."""
    _LAST_WARN.clear()
