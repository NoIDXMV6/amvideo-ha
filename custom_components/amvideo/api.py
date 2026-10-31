"""API-клиент закрытого сервиса А.М.Видео (lk.amvideo-msk.ru).

Протокол восстановлен обратной разработкой приложения А.М.Видео 3.4.1
(Cordova-обёртка, вся логика в assets/www/js/app.js):

  - все запросы: POST form-urlencoded на {base_url} (api/api4.php);
  - ответ: JSON;
  - сессия: после type=login сервер выдаёт sid, который передаётся
    во всех последующих запросах (никаких заголовков/токенов нет);
  - type=open    — открыть шлагбаум (id_shlag, relay);
  - type=panels  — список доступных КПУ;
  - type=video   — получить порт WSS-прокси с MPEG1-TS потоком камеры;
  - type=auth    — проверка сессии + профиль (в т.ч. резервный адрес API).

Модуль не зависит от Home Assistant и может тестироваться отдельно.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Callable

from aiohttp import ClientError, ClientSession, ClientTimeout

from .const import (
    APP_VERSION,
    DEFAULT_BASE_URL,
    DEFAULT_TIMEOUT,
    STREAM_HOST_TEMPLATE,
)

_LOGGER = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
    "AMVideoApp/3.4.1",
    "X-Requested-With": "ru.moscow.shlagbaum",
}


class AmVideoError(Exception):
    """Базовая ошибка API А.М.Видео."""


class AmVideoAuthError(AmVideoError):
    """Ошибка аутентификации / истёкшая сессия."""


class AmVideoPinRequired(AmVideoError):
    """Сервер требует PIN-код устройства (вход с нового устройства)."""


def random_device_key() -> str:
    """Сгенерировать device_key как это делает приложение (uuid v4)."""
    return str(uuid.uuid4())


class AmVideoClient:
    """Асинхронный клиент api4.php."""

    def __init__(
        self,
        session: ClientSession,
        login: str,
        password: str,
        device_key: str | None = None,
        pin: str = "",
        base_url: str = DEFAULT_BASE_URL,
        stream_host: str = STREAM_HOST_TEMPLATE,
        sid: str | None = None,
        reserve_url: str | None = None,
    ) -> None:
        self._session = session
        self._login = "".join(ch for ch in str(login) if ch.isdigit())
        self._password = password
        self._device_key = device_key or random_device_key()
        self._pin = str(pin or "")
        self._base_url = base_url
        self._stream_host = stream_host

        # Сессия может быть восстановлена из сохранённой записи: сервер
        # А.М.Видео держит одну активную сессию на аккаунт, поэтому каждый
        # повторный login завершает сессию приложения на телефоне.
        # Клиент переиспользует sid до тех пор, пока сервер его принимает.
        self.sid: str | None = sid
        self.reserve_url: str | None = reserve_url
        self.profile: dict[str, Any] = {}

    # ------------------------------------------------------------------ #
    # Низкоуровневые запросы
    # ------------------------------------------------------------------ #
    async def _post(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        timeout = ClientTimeout(total=DEFAULT_TIMEOUT)
        try:
            async with self._session.post(
                url, data=payload, headers=_HEADERS, timeout=timeout
            ) as resp:
                if resp.status != 200:
                    raise AmVideoError(f"HTTP {resp.status} от {url}")
                return await resp.json(content_type=None)
        except (ClientError, asyncio.TimeoutError, OSError) as err:
            raise AmVideoError(f"Нет связи с {url}: {err}") from err

    async def _request(self, type_: str, **payload: Any) -> dict[str, Any]:
        """POST с автоматическим переключением на резервный сервер."""
        data = {"type": type_, **payload}
        urls = [self._base_url]
        if self.reserve_url and self.reserve_url != self._base_url:
            urls.append(self.reserve_url)
        last_err: Exception | None = None
        for url in urls:
            try:
                return await self._post(url, data)
            except AmVideoError as err:
                last_err = err
                _LOGGER.debug("Запрос %s к %s не удался: %s", type_, url, err)
        raise AmVideoError(f"Запрос '{type_}' не выполнен: {last_err}")

    @staticmethod
    def _raise_no_access(data: dict[str, Any]) -> None:
        """result=false + «нет доступа» — сессия истекла."""
        if not data.get("result") and "доступ" in str(data.get("str", "")):
            raise AmVideoAuthError(str(data.get("str")))

    # ------------------------------------------------------------------ #
    # Аутентификация
    # ------------------------------------------------------------------ #
    async def login(self) -> str:
        """type=login → sid. Бросает AmVideoPinRequired, если нужен PIN."""
        result = await self._request(
            "login",
            login=self._login,
            password=self._password,
            device_key=self._device_key,
            pin=self._pin,
        )
        if result.get("result") and result.get("sid"):
            self.sid = result["sid"]
            _LOGGER.debug("Успешный вход, sid=%s…", str(self.sid)[:6])
            return self.sid
        if result.get("change_device"):
            raise AmVideoPinRequired(str(result.get("str") or "Требуется PIN"))
        raise AmVideoAuthError(str(result.get("str") or "Неверный логин или пароль"))

    async def request_pin_sms(self, pin_type: str = "device", sms: str = "1") -> dict[str, Any]:
        """type=pin — запросить PIN по SMS (как кнопка «выслать PIN»).

        В приложении кнопка на экране входа шлёт pin_type="device"
        (data-pintype="device" в index.html). Ответ сервера содержит
        result и человекочитаемый str — его проверяет config_flow.
        """
        return await self._request(
            "pin", login=self._login, pin_type=pin_type, sms=sms
        )

    async def auth(self) -> dict[str, Any]:
        """type=auth — проверка сессии, профиль и резервный адрес API."""
        result = await self._request(
            "auth",
            sid=self.sid,
            with_redirect="true",
            app_version=APP_VERSION,
        )
        self._raise_no_access(result)
        if not result.get("result"):
            raise AmVideoAuthError(str(result.get("str") or "Сессия недействительна"))

        extra_link = result.get("extra_link")
        reserve = result.get("reserve")
        api_path = result.get("api")
        if extra_link and reserve and api_path:
            self.reserve_url = f"{extra_link}:{reserve}{api_path}"
        self.profile = result
        return result

    # ------------------------------------------------------------------ #
    # Данные и команды
    # ------------------------------------------------------------------ #
    async def get_panels(self) -> list[dict[str, Any]]:
        """type=panels — список шлагбаумов (КПУ), доступных аккаунту.

        Каждый элемент: {id_shlag, adress, gps, cam, relay}.
        """
        result = await self._request("panels", sid=self.sid)
        self._raise_no_access(result)
        if not result.get("result"):
            raise AmVideoError(str(result.get("str") or "Не удалось получить КПУ"))
        panels = result.get("data") or []
        for panel in panels:
            panel.setdefault("relay", 0)
            panel["cam"] = bool(panel.get("cam"))
        return panels

    async def open_barrier(self, id_shlag: int | str, relay: int | str = 0) -> dict[str, Any]:
        """type=open — команда открытия шлагбаума."""
        result = await self._request(
            "open",
            id_shlag=id_shlag,
            relay=relay if relay not in (None, "") else 0,
            sid=self.sid,
            app_version=APP_VERSION,
        )
        self._raise_no_access(result)
        if not result.get("result"):
            raise AmVideoError(str(result.get("str") or "Шлагбаум не открылся"))
        return result

    async def get_video_port(self, id_shlag: int | str, type_device: str = "panel") -> int:
        """type=video — порт WSS-прокси живого потока (MPEG1-TS).

        Ответ сервера: {status: "ok", wssport: [<порт>]}.
        Подключение: wss://stream.amvideo-msk.ru:<порт> — сервер сразу
        начинает слать бинарный MPEG1-TS (клиент jsmpeg ничего не отправляет).
        """
        result = await self._request(
            "video",
            id_shlag=id_shlag,
            sid=self.sid,
            typeDevice=type_device,
        )
        self._raise_no_access(result)
        if result.get("status") == "ok":
            ports = result.get("wssport") or []
            if ports:
                return int(ports[0])
        raise AmVideoError(
            str(result.get("str") or "Камера недоступна (сервер не выдал порт)")
        )

    def stream_url(self, port: int) -> str:
        """Собрать URL WSS-потока из порта.

        Если в адресе есть шаблон {port} (например ws://host:{port}/ws),
        он подставляется; иначе порт дописывается через двоеточие
        (wss://stream.amvideo-msk.ru:<порт>), как делает приложение.
        """
        if "{port}" in self._stream_host:
            return self._stream_host.format(port=port)
        return f"{self._stream_host}:{port}"

    @property
    def stream_host(self) -> str:
        """Хост WSS-прокси без схемы и порта (для атрибутов камеры)."""
        host = self._stream_host.split("://", 1)[-1]
        return host.split(":", 1)[0].split("/", 1)[0]

    # ------------------------------------------------------------------ #
    # Архив (справочно; в интеграции пока не используется)
    # ------------------------------------------------------------------ #
    async def get_archive(self, channel: str, date: str) -> dict[str, Any]:
        """type=get_archive — сегменты архива за дату (YYYY-MM-DD).

        Каналы берутся из profile['nvr'] (поле channel).
        """
        result = await self._request(
            "get_archive", sid=self.sid, channel=channel, date=date
        )
        self._raise_no_access(result)
        return result
