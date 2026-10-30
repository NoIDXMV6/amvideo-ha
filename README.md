# Интеграция «А.М.Видео» для Home Assistant

![Баннер](docs/banner.png)

Управление шлагбаумами (КПУ) и просмотр живого видео с камер сервиса
**А.М.Видео** (`lk.amvideo-msk.ru`) из Home Assistant.

Интеграция построена на протоколе, восстановленном обратной разработкой
официального приложения **А.М.Видео 3.4.1** (Android, пакет
`ru.moscow.shlagbaum`, XAPK от APKPure). Приложение — Cordova-обёртка,
вся логика находится в JavaScript (`assets/www/js/app.js`), поэтому
протокол восстановлен полностью и проверен живыми запросами к серверу.

---

## Возможности

| Сущность | Описание |
|---|---|
| `button.*_otkryt` | Кнопка «Открыть» для каждого доступного вам КПУ |
| `cover.*_shlagbaum` | Шлагбаум как cover: команда открытия, «предполагаемое» состояние (открывается → открыто → закрыто) |
| `camera.*_kamera` | Живая камера КПУ: MPEG1-TS из WebSocket-прокси декодируется ffmpeg и отдаётся как MJPEG (снимки + поток в more-info) |

- Сервис `amvideo.open_barrier` — открыть шлагбаум из автоматизаций.
- Автоматический повторный вход при истечении сессии (reauth-flow).
- Защита от повторных команд (как в приложении — 20 секунд, настраивается).
- Опрос сервера раз в 5 минут (настраивается) — сессия не «умирает».
- Опции: интервал опроса, тайминги состояния, кулдаун, длительность видео.

## Установка

### Через HACS (рекомендуется)

[![Открыть в HACS](https://img.shields.io/badge/HACS-Установить-0366d6?logo=homeassistant&logoColor=white)](https://my.home-assistant.io/redirect/hacs_repository/?owner=NoIDXMV6&repository=amvideo-ha&category=integration)

1. Установите [HACS](https://hacs.xyz/docs/use/download/download/), если ещё не установлен.
2. **HACS → ⋮ → Добавить свой репозиторий (Custom repository)**.
3. Вставьте адрес: `https://github.com/NoIDXMV6/amvideo-ha`, категория — **Интеграция**.
4. Установите «А.М.Видео (шлагбаумы)» и перезапустите Home Assistant.
5. **Настройки → Устройства и службы → Добавить интеграцию → А.М.Видео**.

### Вручную

1. Скопируйте папку `custom_components/amvideo` в `config/custom_components/`
   вашего Home Assistant (через Samba/Studio Code Server или git).
2. Перезапустите Home Assistant.
3. **Настройки → Устройства и службы → Добавить интеграцию → А.М.Видео**.
4. Введите телефон (как в приложении) и пароль.
   - Если сервер требует PIN для нового устройства (а новое устройство
     здесь всегда), интеграция **сама вышлет PIN по SMS** — просто введите
     полученный код на следующем шаге (действует ~3 минуты).
   - Код не пришёл — отметьте «Выслать новый PIN по SMS» и повторите.
5. Готово: на каждое устройство добавится по устройству («КПУ …»),
   кнопке, шлагбауму и камере. Дальнейшие входы проходят без PIN —
   сервер запоминает устройство (device_key сохраняется в записи).

Требования: Home Assistant 2024.x–2025.x, **ffmpeg** (в HA OS / HA Container
установлен по умолчанию; в HA Core — поставьте пакет вручную).

## Использование в автоматизациях

```yaml
# Кнопка (прямой вызов сервиса)
automation:
  - alias: Открыть шлагбаум по прибытии
    triggers:
      - trigger: device_tracker
        entity_id: device_tracker.my_phone
        to: "home"
    actions:
      - action: button.press
        target:
          entity_id: button.ul_testovaya_1_otkryt

# или через фирменный сервис:
      - action: amvideo.open_barrier
        target:
          entity_id: cover.ul_testovaya_1_shlagbaum

# Открыть, показать камеру на телевизоре и закрыть карточку через 30 секунд
script:
  pribytie:
    sequence:
      - action: amvideo.open_barrier
        target: {entity_id: cover.shlagbaum}
      - action: camera.play_stream   # если настроен stream-прокси
        ...
```

Lovelace-карточка для видеопотока:

```yaml
type: picture-entity
entity: camera.ul_testovaya_1_kamera
camera_view: live
show_state: false
```

> Камера отдаёт MJPEG в окне more-info / picture-entity. Каждое включение
> просмотра — это отдельная сессия `type=video` у сервера, как и в
> приложении. Длительность ограничена (по умолчанию 10 минут), затем
> порт запрашивается заново.

---

## Восстановленный протокол (технические детали)

### Общие сведения

- Приложение: Cordova, вся логика в `assets/www/js/app.js` (jQuery + jQuery Mobile).
- API: **POST `https://lk.amvideo-msk.ru/api/api4.php`**, тело — обычная
  form-urlencoded форма, ответ — JSON. Никаких заголовков авторизации,
  подписей или токенов нет: единственный идентификатор сессии — поле `sid`.
- Резервный сервер: ответ `auth` содержит `extra_link`, `reserve`, `api` —
  приложение собирает `extra_url = extra_link + ":" + reserve + api`
  и переключается на него при сбое основного.
- Push: FCM (senderID `908201477201`), привязка — `type=reg_push`
  (на работу интеграции не влияет).

### 1. Аутентификация

```
POST /api/api4.php
type=login
login=7XXXXXXXXXX        # только цифры, 11 знаков
password=...             # пароль
device_key=...           # UUID устройства; на Android — device.uuid,
                         # генерируется один раз и хранится в localStorage
pin=1234                 # 4-значный PIN устройства; пусто при первом входе
```

Ответ:

```json
{"result": true, "sid": "<строка сессии>"}
```

- `sid` далее передаётся во **всех** запросах (`sid=<sid>`) — это и есть
  «токен». Срок жизни сервером точно не disclosed: приложение повторно
  вызывает `type=auth` при каждом возврате из фона (>1 часа) и хранит
  `sid` бессрочно; интеграция продлевает сессию опросом и делает
  повторный `login` при её истечении.
- Если сервер отвечает `{"result": false, "change_device": true}` —
  вход с нового устройства требует PIN. PIN запрашивается через:

```
type=pin, login=..., pin_type=login|reg|forgot, sms=0|1
```

  (кнопка «Выслать PIN» в приложении; SMS приходит на номер аккаунта),
  после чего `login` повторяется с заполненным `pin`.
- Проверка сессии и профиль: `type=auth, sid=..., with_redirect=true,
  app_version=3.4.1` → `{result, fio, number, panel_access, video_on,
  gps_on, map, nvr: [...], extra_link, reserve, api, my_objects, ...}`.

### 2. Список шлагбаумов

```
POST type=panels, sid=...
→ {"result": true, "data": [
     {"id_shlag": 1234, "adress": "ул. ..., д. ...",
      "gps": "55.75,37.61", "cam": 1, "relay": 1}, ...]}
```

- `id_shlag` — идентификатор КПУ;
- `relay` — номер реле (передаётся в команду открытия);
- `cam` — есть ли камера (показывать кнопку видео).

### 3. Открытие шлагбаума

```
POST type=open, id_shlag=1234, relay=1, sid=..., app_version=3.4.1
→ {"result": true, "str": "открыто"}    # текст ответа показывается в UI
```

Приложение блокирует повторное нажатие на 20 секунд (чисто клиентский
кулдаун — интеграция повторяет его). Открытие «из окна видео» передаёт
`relay=0`.

### 4. Живой видеопоток

```
POST type=video, id_shlag=1234, sid=..., typeDevice=panel
→ {"status": "ok", "wssport": ["PORT"]}   # порт WSS-прокси под сессию
   {"status": "error", "str": "..."}      # камеры нет / недоступна
```

Затем приложение создаёт `JSMpeg.Player("wss://stream.amvideo-msk.ru:" + port)`
— клиент jsmpeg **ничего не отправляет** в WebSocket: сервер сразу начинает
слать бинарный поток **MPEG1 в контейнере MPEG-TS** (демультиплексор TS в
jsmpeg), который рендерится на canvas. Автостоп — через 60 секунд
(клиентский таймер; сервер, судя по всему, рвёт соединение сам).

Интеграция делает то же самое, но вместо canvas:

```
WSS → pipe → ffmpeg -f mpegts -i pipe:0 -f mjpeg -q:v 5 pipe:1 → JPEG-кадры
```

кадры отдаются в Home Assistant (снимок `async_camera_image` и MJPEG-поток
`handle_async_mjpeg_stream`). Порт живёт ограниченное время, поэтому при
обрыве поток запрашивается заново (`type=video` → новый порт).

### 5. Архив видео (справочно, в интеграцию не включено)

- Каналы: `auth` → `nvr: [{id_shlag, channel, adress, alias_name, ...}]`;
- Сегменты за день: `type=get_archive, sid, channel, date=YYYY-MM-DD`
  → `{calendarArchive: [{date}], videoArchiveData: [{start_date_time, duration, url}]}`;
- Скачивание: `https://unity-nvr.amvideo-msk.ru/download.php?file=<base64(url)>`
  (в URL поддерживаются `?start=...&end=...` в секундах).

### 6. Прочие команды API (для полноты)

`reg, forgot, get_agreement, confirm_agreement, total` (гостевые заявки),
`visit_types, objects, my_objects, nomerok, add_car, delete_car,
tech_ticket, tech_list, add_tech_comment, add_tech_file, profile,
recreate, new, edit, disable` (заявки), `pay, paylog, read_notes,
metrika, reload, reg_push`. Все — тот же POST c `sid`.

## Лимиты и осторожность

- Это **неофициальная** интеграция: при изменении API сервисом она может
  перестать работать. Протокол не документирован.
- Не нагружайте сервер: интервал опроса меньше 60 с не имеет смысла,
  длительные фоновые видеопотоки создают нагрузку на прокси-сервер и
  могут быть ограничены/заблокированы со стороны А.М.Видео.
- `device_key` интеграция хранит в конфигурации: сервер видит её как
  «устройство». При необходимости можно задать свой постоянный UUID.
- Пароль хранится в конфигурационной записи Home Assistant (стандартное
  поведение для cloud-интеграций).

### Технические ключи конфигурации (в данных config entry)

| Ключ | Назначение |
|---|---|
| `base_url` | Адрес API (по умолчанию `https://lk.amvideo-msk.ru/api/api4.php`) — поле есть в форме настройки |
| `stream_host` | Адрес видеопрокси (по умолчанию `wss://stream.amvideo-msk.ru:{port}`); поддерживается шаблон `{port}` — редактируется в `.storage/core.config_entries` |

## Отладка

```yaml
logger:
  logs:
    custom_components.amvideo: debug
```

## Структура

```
custom_components/amvideo/
├── manifest.json      # манифест интеграции
├── const.py           # константы (адреса серверов, дефолты)
├── api.py             # клиент api4.php (aiohttp)
├── stream.py          # MPEG1-TS/WS → ffmpeg → JPEG (без зависимостей от HA)
├── coordinator.py     # DataUpdateCoordinator: опрос + keep-alive сессии
├── config_flow.py     # UI-настройка (+ PIN-шаг, reauth, опции)
├── button.py          # кнопки открытия
├── cover.py           # шлагбаумы (cover, предполагаемое состояние)
├── camera.py          # живые камеры
├── services.yaml      # amvideo.open_barrier
└── translations/      # ru, en
```
