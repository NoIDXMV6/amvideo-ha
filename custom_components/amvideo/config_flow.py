"""Процесс добавления интеграции А.М.Видео через UI.

Повторяет сценарий входа приложения:
1) type=login с телефоном, паролем и device_key;
2) если сервер ответил change_device=true — требуется 4-значный PIN
   устройства (его можно выслать по SMS, в приложении это кнопка
   «Выслать PIN»: type=pin, pin_type=login, sms=1).
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import voluptuous as vol

from .api import AmVideoAuthError, AmVideoClient, AmVideoError, AmVideoPinRequired, random_device_key
from .const import (
    CONF_BASE_URL,
    CONF_DEVICE_KEY,
    CONF_PIN,
    COOLDOWN,
    DEFAULT_BASE_URL,
    DEFAULT_COOLDOWN,
    DEFAULT_OPEN_DURATION,
    DEFAULT_OPENING_TIME,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_STREAM_DURATION,
    DOMAIN,
    OPEN_DURATION,
    OPENING_TIME,
    SCAN_INTERVAL,
    STREAM_DURATION,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Required(CONF_DEVICE_KEY, default=random_device_key()): str,
        vol.Optional(CONF_PIN, default=""): str,
        vol.Optional(CONF_BASE_URL, default=DEFAULT_BASE_URL): str,
    }
)

STEP_PIN_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PIN): str,
        vol.Required("send_sms", default=False): bool,
    }
)

OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Required(SCAN_INTERVAL, default=DEFAULT_POLL_INTERVAL): vol.All(
            vol.Coerce(int), vol.Range(min=60, max=3600)
        ),
        vol.Required(OPENING_TIME, default=DEFAULT_OPENING_TIME): vol.All(
            vol.Coerce(int), vol.Range(min=3, max=60)
        ),
        vol.Required(OPEN_DURATION, default=DEFAULT_OPEN_DURATION): vol.All(
            vol.Coerce(int), vol.Range(min=5, max=120)
        ),
        vol.Required(COOLDOWN, default=DEFAULT_COOLDOWN): vol.All(
            vol.Coerce(int), vol.Range(min=5, max=120)
        ),
        vol.Required(STREAM_DURATION, default=DEFAULT_STREAM_DURATION): vol.All(
            vol.Coerce(int), vol.Range(min=60, max=3600)
        ),
    }
)


async def validate_login(hass, data: dict[str, Any]) -> AmVideoClient:
    """Попытка входа; возвращает рабочий клиент."""
    session = async_get_clientsession(hass)
    client = AmVideoClient(
        session,
        login=data[CONF_USERNAME],
        password=data[CONF_PASSWORD],
        device_key=data.get(CONF_DEVICE_KEY),
        pin=data.get(CONF_PIN, ""),
    )
    await client.login()
    await client.auth()
    return client


class AmVideoConfigFlow(ConfigFlow, domain=DOMAIN):
    """Обработка config flow."""

    VERSION = 1

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            await self.async_set_unique_id(str(user_input[CONF_USERNAME]))
            self._abort_if_unique_id_configured()
            self.data = dict(user_input)
            try:
                await validate_login(self.hass, self.data)
            except AmVideoPinRequired:
                return await self.async_step_pin()
            except AmVideoAuthError as err:
                errors["base"] = "invalid_auth"
                _LOGGER.debug("Ошибка входа: %s", err)
            except AmVideoError as err:
                errors["base"] = "cannot_connect"
                _LOGGER.debug("Ошибка связи: %s", err)
            else:
                return self.async_create_entry(title="А.М.Видео", data=self.data)
        return self.async_show_form(step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors)

    async def async_step_pin(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self.data[CONF_PIN] = str(user_input[CONF_PIN]).strip()
            if user_input.get("send_sms"):
                session = async_get_clientsession(self.hass)
                client = AmVideoClient(
                    session,
                    login=self.data[CONF_USERNAME],
                    password=self.data[CONF_PASSWORD],
                    device_key=self.data.get(CONF_DEVICE_KEY),
                )
                try:
                    await client.request_pin_sms(pin_type="login", sms="1")
                except AmVideoError as err:
                    _LOGGER.debug("SMS с PIN не отправлено: %s", err)
            try:
                await validate_login(self.hass, self.data)
            except AmVideoPinRequired:
                errors["base"] = "invalid_pin"
            except AmVideoAuthError:
                errors["base"] = "invalid_pin"
            except AmVideoError:
                errors["base"] = "cannot_connect"
            else:
                return self.async_create_entry(title="А.М.Видео", data=self.data)
        return self.async_show_form(step_id="pin", data_schema=STEP_PIN_SCHEMA, errors=errors)

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        self.data = dict(entry_data)
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self.data.update(user_input)
            try:
                await validate_login(self.hass, self.data)
            except AmVideoPinRequired:
                errors["base"] = "invalid_pin"
            except AmVideoAuthError:
                errors["base"] = "invalid_auth"
            except AmVideoError:
                errors["base"] = "cannot_connect"
            else:
                entry = self._get_reauth_entry()
                self.hass.config_entries.async_update_entry(entry, data=self.data)
                await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_abort(reason="reauth_successful")
        schema = vol.Schema(
            {
                vol.Required(CONF_PASSWORD, default=self.data.get(CONF_PASSWORD, "")): str,
                vol.Optional(CONF_PIN, default=""): str,
            }
        )
        return self.async_show_form(
            step_id="reauth_confirm", data_schema=schema, errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> OptionsFlow:
        return AmVideoOptionsFlow()


class AmVideoOptionsFlow(OptionsFlow):
    """Настройки опроса и таймингов."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        options = self.config_entry.options
        defaults = {
            SCAN_INTERVAL: options.get(SCAN_INTERVAL, DEFAULT_POLL_INTERVAL),
            OPENING_TIME: options.get(OPENING_TIME, DEFAULT_OPENING_TIME),
            OPEN_DURATION: options.get(OPEN_DURATION, DEFAULT_OPEN_DURATION),
            COOLDOWN: options.get(COOLDOWN, DEFAULT_COOLDOWN),
            STREAM_DURATION: options.get(STREAM_DURATION, DEFAULT_STREAM_DURATION),
        }
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(OPTIONS_SCHEMA, defaults),
        )
