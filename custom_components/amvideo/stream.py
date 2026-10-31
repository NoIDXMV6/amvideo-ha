"""Конвейер «MPEG1-TS over WebSocket → ffmpeg → JPEG».

Сервер stream.amvideo-msk.ru после подключения сразу начинает передавать
бинарный поток MPEG1-TS (клиент jsmpeg в приложении ничего не отправляет
и автоматически отключается через 60 секунд).

TLS видеопрокси НЕ проверяется (ssl=False по умолчанию): сертификат,
который отдаёт stream.amvideo-msk.ru, выпущен на unity-nvr.amvideo-msk.ru
(проверено openssl, 2026-11), а приложение А.М.Видео эту проверку не
выполняет — с проверкой по умолчанию aiohttp рвал соединение до первого
байта видео (пустое окно камеры без ошибок в журнале).

Модуль не зависит от Home Assistant.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import time
from typing import AsyncIterator, Awaitable, Callable

import aiohttp

from .api import AmVideoError
from .const import DEFAULT_FIRST_FRAME_TIMEOUT, DEFAULT_SNAPSHOT_CACHE
from .diag import warn_limited

_LOGGER = logging.getLogger(__name__)

# stdin: MPEG-TS от сервера; stdout: MJPEG-кадры
FFMPEG_ARGS = (
    "ffmpeg",
    "-hide_banner",
    "-loglevel",
    "error",
    "-fflags",
    "nobuffer",
    "-flags",
    "low_delay",
    "-f",
    "mpegts",
    "-i",
    "pipe:0",
    "-f",
    "mjpeg",
    "-q:v",
    "5",
    "pipe:1",
)

_SOI = b"\xff\xd8\xff"
_EOI = b"\xff\xd9"


def ffmpeg_available() -> bool:
    """Есть ли ffmpeg в системе (проверяется на каждый вызов)."""
    return shutil.which("ffmpeg") is not None


async def _pump_ws_to_stdin(ws: aiohttp.ClientWebSocketResponse, stdin: asyncio.StreamWriter) -> None:
    """Перекачивать бинарные фреймы WebSocket в stdin ffmpeg."""
    try:
        async for msg in ws:
            if msg.type is aiohttp.WSMsgType.BINARY:
                stdin.write(msg.data)
                await stdin.drain()
            elif msg.type in (
                aiohttp.WSMsgType.CLOSED,
                aiohttp.WSMsgType.CLOSING,
                aiohttp.WSMsgType.ERROR,
            ):
                break
    except (aiohttp.ClientError, ConnectionError, OSError):
        pass
    finally:
        try:
            stdin.close()
        except (OSError, RuntimeError):
            pass


async def _read_frames(
    stdout: asyncio.StreamReader,
    deadline: float,
    first_frame_timeout: float | None = None,
) -> AsyncIterator[bytes]:
    """Разбивать stdout ffmpeg на отдельные JPEG-кадры (SOI…EOI).

    first_frame_timeout — watchdog: если за это время от сервера не
    пришло НИ ОДНОГО байта (WS подключился, но данных нет), бросаем
    TimeoutError — иначе MJPEG-поток висел бы молча, а пользователь
    видел бы пустое окно без единой записи в журнале.
    """
    buf = b""
    got_data = False
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        if not got_data and first_frame_timeout is not None:
            remaining = min(remaining, first_frame_timeout)
        try:
            chunk = await asyncio.wait_for(
                stdout.read(65536), timeout=max(remaining, 0.1)
            )
        except asyncio.TimeoutError:
            if not got_data and first_frame_timeout is not None:
                raise asyncio.TimeoutError(
                    f"сервер не передаёт видеоданные: за "
                    f"{first_frame_timeout:.0f} с не пришло ни одного байта"
                ) from None
            return
        if not chunk:
            return
        got_data = True
        buf += chunk
        while True:
            start = buf.find(_SOI)
            if start == -1:
                buf = b""
                break
            end = buf.find(_EOI, start + 2)
            if end == -1:
                buf = buf[start:]
                break
            frame = buf[start : end + 2]
            buf = buf[end + 2 :]
            if len(frame) > 64:
                yield frame


class BarrierStream:
    """Живой видеопоток одного шлагбаума.

    get_target — колбэк, запрашивающий у API цель WSS-прокси (type=video):
    "порт" либо "порт/токен" (протокол 2026). Цель выдаётся под сессию и
    живёт ограниченное время, поэтому при обрыве поток запрашивается заново.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        get_target: Callable[[], Awaitable[str]],
        stream_url_for_target: Callable[[str], str],
        max_duration: float = 600,
        snapshot_cache: float = DEFAULT_SNAPSHOT_CACHE,
        ssl_verify: bool = False,
        first_frame_timeout: float = DEFAULT_FIRST_FRAME_TIMEOUT,
    ) -> None:
        self._session = session
        self._get_target = get_target
        self._url_for = stream_url_for_target
        self._max_duration = max_duration
        self._snapshot_cache = snapshot_cache
        # Сертификат видеопрокси не покрывает имя stream.amvideo-msk.ru,
        # приложение его не проверяет — по умолчанию тоже не проверяем.
        self._ssl = ssl_verify if ssl_verify else False
        self._first_frame_timeout = first_frame_timeout

        self._last_frame: bytes | None = None
        self._last_frame_ts = 0.0
        # диагностика последней попытки (для атрибутов камеры)
        self.last_target: str | None = None
        self.last_error: str | None = None
        self.frames_seen: int = 0

    # ------------------------------------------------------------------ #
    async def _run_pipeline(
        self, deadline: float
    ) -> AsyncIterator[bytes]:
        """Одно подключение: запросить цель → WS → ffmpeg → кадры."""
        target = await self._get_target()
        self.last_target = str(target)
        url = self._url_for(target)
        _LOGGER.debug("Подключение к видеопрокси: %s", url)

        if not ffmpeg_available():
            raise RuntimeError("ffmpeg не найден в системе — видео недоступно")

        async with self._session.ws_connect(url, ssl=self._ssl) as ws:
            proc = await asyncio.create_subprocess_exec(
                *FFMPEG_ARGS,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            assert proc.stdin is not None and proc.stdout is not None
            pump = asyncio.create_task(_pump_ws_to_stdin(ws, proc.stdin))
            try:
                async for frame in _read_frames(
                    proc.stdout, deadline, self._first_frame_timeout
                ):
                    self._last_frame = frame
                    self._last_frame_ts = time.monotonic()
                    self.frames_seen += 1
                    yield frame
            finally:
                pump.cancel()
                try:
                    await pump
                except BaseException:  # noqa: BLE001  # подавляем отмену/ошибки завершения
                    pass
                proc.kill()
                try:
                    await proc.wait()
                except (OSError, RuntimeError):
                    pass

    async def frames(self) -> AsyncIterator[bytes]:
        """Непрерывный поток кадров с переподключением.

        Сессия ограничена max_duration секундами; счётчик попыток
        сбрасывается после каждого успешно полученного кадра.
        """
        deadline = time.monotonic() + self._max_duration
        attempts = 0
        max_attempts = 6
        while time.monotonic() < deadline and attempts < max_attempts:
            attempts += 1
            got_frame = False
            try:
                async for frame in self._run_pipeline(deadline):
                    if not got_frame:
                        got_frame = True
                        attempts = 0
                    yield frame
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as err:
                self.last_error = f"{type(err).__name__}: {err}"
                warn_limited(
                    _LOGGER,
                    f"frames_err_{id(self)}",
                    "Видеопоток прерван (%s: %s), переподключение…",
                    type(err).__name__,
                    err,
                )
            except AmVideoError as err:
                self.last_error = str(err)
                # сервер не выдал цель / «нет доступа» / нет связи при
                # запросе type=video — раньше это умирало на DEBUG и окно
                # камеры пустело без единой записи в журнале
                warn_limited(
                    _LOGGER,
                    f"frames_api_{id(self)}",
                    "Видеопоток остановлен: %s (повтор — при следующем "
                    "открытии видео)",
                    err,
                )
                return
            await asyncio.sleep(2)
        if attempts >= max_attempts:
            warn_limited(
                _LOGGER,
                f"frames_giveup_{id(self)}",
                "Видеопоток остановлен после %d неудачных подключений подряд "
                "(сервер не выдал порт или отклонил соединение; повтор — при "
                "следующем открытии видео)",
                max_attempts,
            )

    async def snapshot(self, timeout: float = 12.0) -> bytes | None:
        """Один кадр (для снимка камеры). Кэшируется на snapshot_cache сек."""
        if self._last_frame and time.monotonic() - self._last_frame_ts < self._snapshot_cache:
            return self._last_frame
        try:
            async with asyncio.timeout(timeout):
                async for frame in self._run_pipeline(
                    time.monotonic() + timeout
                ):
                    return frame
        except (
            TimeoutError, aiohttp.ClientError, OSError, RuntimeError, AmVideoError
        ) as err:
            self.last_error = f"{type(err).__name__}: {err}"
            warn_limited(
                _LOGGER,
                f"snapshot_err_{id(self)}",
                "Снимок камеры не получен: %s: %s (если так повторяется — "
                "видео у сервера недоступно для этого аккаунта/КПУ)",
                type(err).__name__,
                err,
            )
        return None
