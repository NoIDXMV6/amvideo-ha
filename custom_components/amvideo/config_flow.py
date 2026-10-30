"""Процесс добавления интеграции А.М.Видео через UI.

Повторяет сценарий входа приложения:
1) пользователь вводит телефон и пароль;
2) интеграция пытается войти (type=login без PIN); сервер отвечает
   change_device=true — вход с нового устройства требует 4-значный PIN;
3) интеграция САМА высылает PIN по SMS (type=pin, pin_type=device,
   sms=0 — ровно как первое нажатие кнопки «Запросить ПИН» в приложении);
4) пользователь вводит код из SMS — вход повторяется с PIN
   (код действует ~3 минуты, повторная высылка — sms=1).

device_key генерируется автоматически и сохраняется в записи: после
первого успешного входа сервер запоминает устройство, и повторные
входы проходят без PIN.
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

from .api import (
    AmVideoAuthError,
    AmVideoClient,
    AmVideoError,
    AmVideoPinRequired,
    random_device_key,
)
from .const import (
    CONF_DEVICE_KEY,
    CONF_PIN,
    CONF_RESERVE_URL,
    CONF_SID,
    COOLDOWN,
    DOMAIN,
    OPEN_DURATION,
    OPENING_TIME,
    SCAN_INTERVAL,
    STREAM_DURATION,
    DEFAULT_COOLDOWN,
    DEFAULT_OPEN_DURATION,
    DEFAULT_OPENING_TIME,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_STREAM_DURATION,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)

STEP_PIN_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PIN): str,
        vol.Required("resend", default=False): bool,
    }
)

STEP_REAUTH_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PASSWORD): str,
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
    """Попытка входа; возвращает клиент с полученным sid."""
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
    # Запоминаем сессию и резервный адрес: после рестарта HA интеграция
    # продолжит работать по этому sid без повторного login (который
    # завершил бы сессию приложения на телефоне).
    data[CONF_SID] = client.sid
    if client.reserve_url:
        data[CONF_RESERVE_URL] = client.reserve_url
    return client


class AmVideoConfigFlow(ConfigFlow, domain=DOMAIN):
    """Обработка config flow."""

    VERSION = 1

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}
        self._reauth_entry = None

    # ------------------------------------------------------------------ #
    # Высылка PIN по SMS (type=pin, как кнопка «Запросить ПИН»)
    # ------------------------------------------------------------------ #
    async def _send_pin(self, sms: str) -> tuple[bool, str]:
        """Запросить PIN. Возвращает (успех, текст для пользователя)."""
        client = AmVideoClient(
            async_get_clientsession(self.hass),
            login=self.data[CONF_USERNAME],
            password=self.data[CONF_PASSWORD],
            device_key=self.data.get(CONF_DEVICE_KEY),
        )
        try:
            result = await client.request_pin_sms(pin_type="device", sms=sms)
        except AmVideoError as err:
            return False, f"Запрос PIN не выполнен: {err}"
        if result.get("result"):
            return True, str(
                result.get("str")
                or "ПИН отправлен по SMS. Введите код и отправьте форму."
            )
        # например: «Превышен лимит отправки ПИН (не более 3 в сутки)»
        return False, str(result.get("str") or "Сервер не выслал PIN")

    # ------------------------------------------------------------------ #
    # Шаг 1: телефон + пароль
    # ------------------------------------------------------------------ #
    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        status = (
            "Введите телефон (7XXXXXXXXXX) и пароль — те же, что в "
            "приложении А.М.Видео. Если сервер потребует PIN для нового "
            "устройства, интеграция сама вышлет код по SMS. "
            "Обратите внимание: сервер держит одну активную сессию на "
            "аккаунт, поэтому после входа интеграции приложение на телефоне "
            "попросит войти заново — это нормально, дальше они работают параллельно."
        )
        if user_input is not None:
            login = "".join(ch for ch in str(user_input[CONF_USERNAME]) if ch.isdigit())
            await self.async_set_unique_id(login)
            self._abort_if_unique_id_configured()
            self.data = dict(user_input)
            self.data[CONF_USERNAME] = login
            self.data.setdefault(CONF_DEVICE_KEY, random_device_key())
            self.data[CONF_PIN] = ""
            try:
                await validate_login(self.hass, self.data)
            except AmVideoPinRequired:
                # Новый код доставляется как в приложении — по SMS
                ok, status = await self._send_pin(sms="0")
                if ok:
                    status = (
                        f"{status} Введите код из SMS (действует ~3 минуты) "
                        "и отправьте форму."
                    )
                    self.data["pin_status"] = status
                    return await self.async_step_pin()
                errors["base"] = "pin_request_failed"
                self.data["pin_status"] = status
                return await self.async_step_pin()
            except AmVideoAuthError as err:
                errors["base"] = "invalid_auth"
                status = f"Ошибка входа: {err}"
                _LOGGER.debug("Ошибка входа: %s", err)
            except AmVideoError as err:
                errors["base"] = "cannot_connect"
                status = f"Нет связи с сервером: {err}"
                _LOGGER.debug("Ошибка связи: %s", err)
            else:
                # device_key сервер запомнил — вход без PIN; PIN не сохраняем
                self.data[CONF_PIN] = ""
                return self.async_create_entry(title="А.М.Видео", data=self.data)
        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_SCHEMA,
            errors=errors or None,
            description_placeholders={"status": status},
        )

    # ------------------------------------------------------------------ #
    # Шаг 2: PIN из SMS
    # ------------------------------------------------------------------ #
    async def async_step_pin(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ввод PIN, высланного по SMS.

        Галочка «Выслать новый PIN» повторяет запрос (sms=1, как повторное
        нажатие кнопки в приложении) и НЕ выполняет вход — новый PIN
        перезаписывает предыдущий.
        """
        errors: dict[str, str] = {}
        status = str(
            self.data.get("pin_status")
            or "Введите PIN, высланный по SMS, и отправьте форму. "
            "Если код не пришёл — отметьте «Выслать новый PIN»."
        )
        if user_input is not None:
            if user_input.get("resend"):
                ok, status = await self._send_pin(sms="1")
                if ok:
                    status = f"{status} Введите новый код и отправьте форму."
                else:
                    errors["base"] = "pin_request_failed"
            elif str(user_input.get(CONF_PIN, "")).strip():
                self.data[CONF_PIN] = str(user_input[CONF_PIN]).strip()
                try:
                    await validate_login(self.hass, self.data)
                except AmVideoPinRequired as err:
                    errors["base"] = "invalid_pin"
                    status = (
                        f"Сервер отклонил PIN: {err}. PIN действует ~3 минуты — "
                        "запросите новый («Выслать новый PIN») и введите его сразу."
                    )
                except AmVideoAuthError as err:
                    errors["base"] = "invalid_auth"
                    status = f"Ошибка входа: {err}"
                except AmVideoError as err:
                    errors["base"] = "cannot_connect"
                    status = f"Нет связи с сервером: {err}"
                else:
                    self.data[CONF_PIN] = ""
                    if self._reauth_entry is not None:
                        entry = self._reauth_entry
                        self.hass.config_entries.async_update_entry(
                            entry, data=self.data
                        )
                        await self.hass.config_entries.async_reload(entry.entry_id)
                        return self.async_abort(reason="reauth_successful")
                    return self.async_create_entry(title="А.М.Видео", data=self.data)
        self.data["pin_status"] = status
        return self.async_show_form(
            step_id="pin",
            data_schema=STEP_PIN_SCHEMA,
            errors=errors or None,
            description_placeholders={"status": status},
        )

    # ------------------------------------------------------------------ #
    # Reauth: пароль → (при необходимости) PIN
    # ------------------------------------------------------------------ #
    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        self._reauth_entry = self._get_reauth_entry()
        self.data = dict(entry_data)
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        status = (
            "Сессия недействительна (срок истёк либо её перехватил свежий "
            "вход — например, вход в приложение на телефоне). Введите "
            "актуальный пароль для нового входа. Если сервер потребует "
            "PIN, интеграция сама вышлет код по SMS. "
            "Внимание: после этого входа приложение на телефоне попросит "
            "авторизоваться заново — это ограничение сервера А.М.Видео."
        )
        if user_input is not None:
            self.data[CONF_PASSWORD] = user_input[CONF_PASSWORD]
            self.data[CONF_PIN] = ""
            try:
                await validate_login(self.hass, self.data)
            except AmVideoPinRequired:
                ok, status = await self._send_pin(sms="0")
                if ok:
                    status = (
                        f"{status} Введите код из SMS (действует ~3 минуты) "
                        "и отправьте форму."
                    )
                else:
                    errors["base"] = "pin_request_failed"
                self.data["pin_status"] = status
                return await self.async_step_pin()
            except AmVideoAuthError as err:
                errors["base"] = "invalid_auth"
                status = f"Ошибка входа: {err}"
            except AmVideoError as err:
                errors["base"] = "cannot_connect"
                status = f"Нет связи с сервером: {err}"
            else:
                entry = self._reauth_entry
                self.data[CONF_PIN] = ""
                self.hass.config_entries.async_update_entry(entry, data=self.data)
                await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_abort(reason="reauth_successful")
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_REAUTH_SCHEMA,
            errors=errors or None,
            description_placeholders={"status": status},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> OptionsFlow:
        return AmVideoOptionsFlow()


class AmVideoOptionsFlow(OptionsFlow):
    """Меню: параметры опроса и повторная авторизация без удаления."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Меню настроек интеграции."""
        return self.async_show_menu(step_id="init", menu_options=["options", "reauth"])

    async def async_step_options(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Параметры опроса и таймингов."""
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
            step_id="options",
            data_schema=self.add_suggested_values_to_schema(OPTIONS_SCHEMA, defaults),
        )

    async def async_step_reauth(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Запустить повторную авторизацию без удаления интеграции."""
        entry = self.config_entry
        try:
            # HA ≥ 2024.2: штатный запуск reauth-flow (с дедупликацией)
            entry.async_start_reauth(self.hass)
        except AttributeError:
            self.hass.async_create_task(
                self.hass.config_entries.flow.async_init(
                    DOMAIN,
                    context={
                        "source": "reauth",
                        "entry_id": entry.entry_id,
                        "unique_id": entry.unique_id,
                        "title_placeholders": {},
                    },
                    data=entry.data,
                )
            )
        return self.async_abort(reason="reauth_started")
