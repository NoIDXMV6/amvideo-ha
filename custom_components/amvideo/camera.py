"""Камеры шлагбаумов А.М.Видео.

Живой поток — MPEG1-TS через WebSocket-прокси (wss://stream.amvideo-msk.ru:порт).
Порт выдаётся сервером по команде type=video под конкретную сессию и живёт
ограниченное время, поэтому интеграция:

  1. запрашивает порт (type=video, id_shlag, sid);
  2. подключается к wss://…:порт и перекачивает бинарный поток в ffmpeg;
  3. ffmpeg декодирует MPEG1 и отдаёт MJPEG-кадры;
  4. кадры отдаются в HA (снимок / MJPEG-стрим в more-info).

Если камера у КПУ отсутствует (panels[].cam = false) — сущность не создаётся.
"""
from __future__ import annotations

import logging
from typing import Any

from aiohttp import web
from homeassistant.components.camera import Camera
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DEFAULT_STREAM_DURATION,
    DOMAIN,
    MANUFACTURER,
    MODEL_BARRIER,
    STREAM_DURATION,
)
from .coordinator import AmVideoCoordinator
from .stream import BarrierStream, ffmpeg_available
from .diag import warn_limited

_LOGGER = logging.getLogger(__name__)

_BOUNDARY = "amvideoframe"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Создать камеры для КПУ, у которых есть камера."""
    coordinator: AmVideoCoordinator = entry.runtime_data
    if not ffmpeg_available():
        _LOGGER.error(
            "ffmpeg не найден в системе — камеры А.М.Видео будут недоступны. "
            "Установите ffmpeg (в HA OS/Container он есть по умолчанию)."
        )
    entities: list[AmVideoCamera] = []
    for panel in coordinator.data.get("panels", []):
        if panel.get("cam"):
            entities.append(AmVideoCamera(coordinator, entry.entry_id, panel))
    async_add_entities(entities)


class AmVideoCamera(Camera):
    """Камера КПУ: MPEG1-TS over WebSocket → ffmpeg → MJPEG."""

    _attr_has_entity_name = True
    _attr_name = "Камера"

    def __init__(
        self,
        coordinator: AmVideoCoordinator,
        entry_id: str,
        panel: dict[str, Any],
    ) -> None:
        Camera.__init__(self)
        self._coordinator = coordinator
        self._panel = panel
        id_shlag = str(panel["id_shlag"])
        self._id_shlag = id_shlag
        self._attr_unique_id = f"{entry_id}_cam_{id_shlag}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"shlag_{id_shlag}")},
            name=panel.get("adress") or f"КПУ {id_shlag}",
            manufacturer=MANUFACTURER,
            model=MODEL_BARRIER,
        )
        session = async_get_clientsession(coordinator.hass)
        self._stream = BarrierStream(
            session,
            get_port=lambda: coordinator.client.get_video_port(id_shlag),
            stream_url_for_port=coordinator.client.stream_url,
            max_duration=coordinator.entry.options.get(
                STREAM_DURATION, DEFAULT_STREAM_DURATION
            ),
        )

    @property
    def available(self) -> bool:
        return self._coordinator.last_update_success

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "id_shlag": self._panel.get("id_shlag"),
            "adress": self._panel.get("adress"),
            "stream_type": "MPEG1-TS over WebSocket (jsmpeg)",
        }

    @property
    def frame_interval(self) -> float:
        """Интервал кадров для резервного still-стрима."""
        return 1.0

    # ------------------------------------------------------------------ #
    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Одиночный кадр (снимок)."""
        if not ffmpeg_available():
            warn_limited(
                _LOGGER,
                "no_ffmpeg",
                "Камера %s: ffmpeg не найден в системе — кадры недоступны. "
                "Установите ffmpeg (в HA OS/Container он есть по умолчанию).",
                self._panel.get("adress"),
            )
            return None
        try:
            return await self._stream.snapshot()
        except Exception as err:  # noqa: BLE001
            warn_limited(
                _LOGGER,
                f"snapshot_{self._id_shlag}",
                "Снимок %s не удался: %s: %s",
                self._panel.get("adress"),
                type(err).__name__,
                err,
            )
            return None

    async def handle_async_mjpeg_stream(self, request: web.Request) -> web.StreamResponse | None:
        """Живой MJPEG-поток из MPEG1-канала."""
        if not ffmpeg_available():
            warn_limited(
                _LOGGER,
                "no_ffmpeg",
                "Камера %s: ffmpeg не найден в системе — видео недоступно.",
                self._panel.get("adress"),
            )
            return None
        response = web.StreamResponse()
        response.content_type = (
            f"multipart/x-mixed-replace; boundary={_BOUNDARY}"
        )
        await response.prepare(request)

        try:
            async for frame in self._stream.frames():
                part = (
                    f"--{_BOUNDARY}\r\n"
                    "Content-Type: image/jpeg\r\n"
                    f"Content-Length: {len(frame)}\r\n\r\n"
                ).encode("ascii") + frame + b"\r\n"
                await response.write(part)
                if request.transport is None or request.transport.is_closing():
                    break
        except (ConnectionResetError, ConnectionError):
            _LOGGER.debug("Клиент отключился от потока %s", self.entity_id)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Поток %s прерван: %s", self.entity_id, err)
        finally:
            try:
                await response.write_eof()
            except (ConnectionResetError, ConnectionError):
                pass
        return response

    async def async_will_remove_from_hass(self) -> None:
        """Очистки не требуются: конвейер собирается на лету."""
