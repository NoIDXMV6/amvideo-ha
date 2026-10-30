"""Координатор данных интеграции А.М.Видео.

Опрашивает сервер (type=panels) и бережно относится к сессии: сервер
держит одну активную сессию на аккаунт, поэтому каждый повторный login
завершает сессию приложения на телефоне. Интеграция:

  - переиспользует sid, сохранённый в записи конфигурации;
  - держит сессию живой keep-alive-запросом type=auth раз в 55 минут
    (как приложение, которое посылает type=auth раз в час);
  - НИКОГДА не выполняет login автоматически: если сервер ответил
    «нет доступа», поднимается ConfigEntryAuthFailed — HA предлагает
    повторную авторизацию в настройках интеграции.

Здесь же хранится время последней команды открытия для реализации
антидребезга (в приложении — 20 секунд).
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

from .api import AmVideoAuthError, AmVideoClient, AmVideoError, AmVideoPinRequired
from .const import (
    CONF_BASE_URL,
    CONF_DEVICE_KEY,
    CONF_PIN,
    CONF_RESERVE_URL,
    CONF_SID,
    CONF_STREAM_HOST,
    COOLDOWN,
    DEFAULT_BASE_URL,
    DEFAULT_COOLDOWN,
    DEFAULT_OPEN_DURATION,
    DEFAULT_OPENING_TIME,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    KEEPALIVE_INTERVAL,
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
            sid=entry.data.get(CONF_SID),
            reserve_url=entry.data.get(CONF_RESERVE_URL),
        )
        self.entry = entry
        self.last_open: dict[str, float] = {}
        self._last_keepalive: float = 0.0

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
        """Опрос сервера; login выполняется ТОЛЬКО вручную (reauth).

        Сессия поддерживается keep-alive (type=auth раз в 55 минут, как
        в приложении). Если сервер ответил «нет доступа» — значит, сессию
        перехватил свежий вход (например, вход в приложение на телефоне)
        либо истёк срок: поднимаем ConfigEntryAuthFailed, HA покажет
        «Требуется повторная авторизация». Автоматический вход запрещён
        специально: он завершал бы сессию приложения на телефоне.
        """
        try:
            if self.client.sid and time.time() - self._last_keepalive > KEEPALIVE_INTERVAL:
                try:
                    await self.client.auth()
                    self._last_keepalive = time.time()
                except AmVideoAuthError:
                    # sid мёртв — ниже panels это подтвердит и поднимет reauth
                    _LOGGER.debug("Keep-alive: сессия недействительна")
            panels = await self.client.get_panels()
            self._last_keepalive = time.time()
        except AmVideoAuthError as err:
            raise ConfigEntryAuthFailed(
                "Сессия А.М.Видео недействительна: "
                f"{err}. Откройте «Настройки → А.М.Видео → Повторная авторизация». "
                "Учтите: новый вход интеграции завершает сессию в приложении "
                "на телефоне — после него приложение попросит войти заново."
            ) from err
        except AmVideoPinRequired as err:
            # сервер забыл устройство — нужен PIN: ведём пользователя в reauth,
            # где интеграция сама вышлет новый код по SMS
            raise ConfigEntryAuthFailed(
                f"Сервер требует PIN устройства: {err}. Обновите вход."
            ) from err
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
