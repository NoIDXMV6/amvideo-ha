"""Координатор данных интеграции А.М.Видео.

Опрашивает сервер (type=panels) и поддерживает сессию:
если sid истёк — автоматически выполняет повторный login по сохранённым
реквизитам. Здесь же хранится время последней команды открытия для
реализации антидребезга (в приложении — 20 секунд).
"""
from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import AmVideoAuthError, AmVideoClient, AmVideoError
from .const import (
    CONF_BASE_URL,
    CONF_DEVICE_KEY,
    CONF_PIN,
    CONF_STREAM_HOST,
    COOLDOWN,
    DEFAULT_BASE_URL,
    DEFAULT_COOLDOWN,
    DEFAULT_OPEN_DURATION,
    DEFAULT_OPENING_TIME,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    OPEN_DURATION,
    OPENING_TIME,
    SCAN_INTERVAL,
    STREAM_HOST_TEMPLATE,
)

_LOGGER = logging.getLogger(__name__)


class AmVideoCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Общий координатор для всех сущностей аккаунта."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        session = async_get_clientsession(hass)
        self.client = AmVideoClient(
            session,
            login=entry.data[CONF_USERNAME],
            password=entry.data[CONF_PASSWORD],
            device_key=entry.data.get(CONF_DEVICE_KEY),
            pin=entry.data.get(CONF_PIN, ""),
            base_url=entry.data.get(CONF_BASE_URL, DEFAULT_BASE_URL),
            stream_host=entry.data.get(CONF_STREAM_HOST, STREAM_HOST_TEMPLATE),
        )
        self.entry = entry
        self.last_open: dict[str, float] = {}

        interval = entry.options.get(SCAN_INTERVAL, DEFAULT_POLL_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry.entry_id}",
            update_interval=timedelta(seconds=interval),
        )

    # ------------------------------------------------------------------ #
    @property
    def opening_time(self) -> int:
        return int(self.entry.options.get(OPENING_TIME, DEFAULT_OPENING_TIME))

    @property
    def open_duration(self) -> int:
        return int(self.entry.options.get(OPEN_DURATION, DEFAULT_OPEN_DURATION))

    @property
    def cooldown(self) -> int:
        return int(self.entry.options.get(COOLDOWN, DEFAULT_COOLDOWN))

    # ------------------------------------------------------------------ #
    async def _async_update_data(self) -> dict[str, Any]:
        """Опрос сервера с автоповтором входа при истёкшей сессии."""
        try:
            try:
                panels = await self.client.get_panels()
            except AmVideoAuthError:
                _LOGGER.info("Сессия истекла — повторный вход")
                await self.client.login()
                await self.client.auth()
                panels = await self.client.get_panels()
        except AmVideoAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except AmVideoError as err:
            raise UpdateFailed(str(err)) from err
        return {"panels": panels}

    # ------------------------------------------------------------------ #
    async def async_open_barrier(self, panel: dict[str, Any]) -> str:
        """Открыть шлагбаум с защитой от повторных команд."""
        id_shlag = str(panel["id_shlag"])
        last = self.last_open.get(id_shlag, 0.0)
        elapsed = time.time() - last
        if elapsed < self.cooldown:
            wait = int(self.cooldown - elapsed)
            raise AmVideoError(
                f"Повторная команда возможна через {wait} с"
            )
        result = await self.client.open_barrier(
            panel["id_shlag"], panel.get("relay", 0)
        )
        self.last_open[id_shlag] = time.time()
        # cover-сущности должны пересчитать предполагаемое состояние сразу,
        # даже если команда была отправлена кнопкой или сервисом
        self.async_update_listeners()
        return str(result.get("str") or "открыто")
