"""Константы интеграции А.М.Видео."""
from __future__ import annotations

DOMAIN = "amvideo"
PLATFORMS = ["button", "cover", "camera"]

MANUFACTURER = "А.М.Видео"
MODEL_BARRIER = "КПУ (шлагбаум)"

# Конфигурация
CONF_DEVICE_KEY = "device_key"
CONF_PIN = "pin"
CONF_BASE_URL = "base_url"
CONF_STREAM_HOST = "stream_host"
CONF_SID = "sid"
CONF_RESERVE_URL = "reserve_url"

# Значения по умолчанию / параметры протокола
APP_VERSION = "3.4.1"
DEFAULT_BASE_URL = "https://lk.amvideo-msk.ru/api/api4.php"
STREAM_WS_HOST = "wss://stream.amvideo-msk.ru"
STREAM_HOST_TEMPLATE = "wss://stream.amvideo-msk.ru:{port}"
DEFAULT_TIMEOUT = 30

# Keep-alive сессии (type=auth) — как в приложении: раз в час активности.
KEEPALIVE_INTERVAL = 3300  # 55 минут

DEFAULT_POLL_INTERVAL = 300
DEFAULT_OPENING_TIME = 10       # секунд "открывается" (исходное положение)
DEFAULT_OPEN_DURATION = 20      # сколько секунд считается открытым (как в приложении)
DEFAULT_COOLDOWN = 20           # защита от повторной команды (как в приложении)
DEFAULT_STREAM_DURATION = 600   # максимальная длительность одной MJPEG-сессии, сек
DEFAULT_SNAPSHOT_CACHE = 10     # кэш кадра для снимков, сек

SCAN_INTERVAL = "scan_interval"
OPENING_TIME = "opening_time"
OPEN_DURATION = "open_duration"
COOLDOWN = "cooldown"
STREAM_DURATION = "stream_duration"
