"""Конвейер «MPEG1-TS over WebSocket → ffmpeg → JPEG».

Сервер stream.amvideo-msk.ru после подключения сразу начинает передавать
бинарный поток MPEG1-TS (клиент jsmpeg в приложении ничего не отправляет
и автоматически отключается через 60 секунд).

Модуль не зависит от Home Assistant.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import time
from typing import AsyncIterator, Awaitable, Callable

import aiohttp

from .const import DEFAULT_SNAPSHOT_CACHE

_LOGGER = logging.getLogger(__name__)

FFMPEG_BINARY = shutil.which("ffmpeg")

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
    """Есть ли ffmpeg в системе."""
    return FFMPEG_BINARY is not None


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


async def _read_frames(stdout: asyncio.StreamReader, deadline: float) -> AsyncIterator[bytes]:
    """Разбивать stdout ffmpeg на отдельные JPEG-кадры (SOI…EOI)."""
    buf = b""
    while time.monotonic() < deadline:
        chunk = await stdout.read(65536)
        if not chunk:
            return
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

    get_port — колбэк, запрашивающий у API свежий порт WSS-прокси
    (type=video). Порт выдаётся под сессию и живёт ограниченное время,
    поэтому при обрыве поток запрашивается заново.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        get_port: Callable[[], Awaitable[int]],
        stream_url_for_port: Callable[[int], str],
        max_duration: float = 600,
        snapshot_cache: float = DEFAULT_SNAPSHOT_CACHE,
    ) -> None:
        self._session = session
        self._get_port = get_port
        self._url_for = stream_url_for_port
        self._max_duration = max_duration
        self._snapshot_cache = snapshot_cache

        self._last_frame: bytes | None = None
        self._last_frame_ts = 0.0

    # ------------------------------------------------------------------ #
    async def _run_pipeline(
        self, deadline: float
    ) -> AsyncIterator[bytes]:
        """Одно подключение: запросить порт → WS → ffmpeg → кадры."""
        port = await self._get_port()
        url = self._url_for(port)
        _LOGGER.debug("Подключение к видеопрокси: %s", url)

        if not ffmpeg_available():
            raise RuntimeError("ffmpeg не найден в системе — видео недоступно")

        async with self._session.ws_connect(url) as ws:
            proc = await asyncio.create_subprocess_exec(
                *FFMPEG_ARGS,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            assert proc.stdin is not None and proc.stdout is not None
            pump = asyncio.create_task(_pump_ws_to_stdin(ws, proc.stdin))
            try:
                async for frame in _read_frames(proc.stdout, deadline):
                    self._last_frame = frame
                    self._last_frame_ts = time.monotonic()
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
                _LOGGER.debug("Видеопоток прерван (%s), переподключение", err)
            except RuntimeError as err:
                _LOGGER.error("%s", err)
                return
            await asyncio.sleep(2)

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
        except (TimeoutError, aiohttp.ClientError, OSError, RuntimeError) as err:
            _LOGGER.debug("Снимок не получен: %s", err)
        return None
