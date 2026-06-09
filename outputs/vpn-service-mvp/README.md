# VLESS Reality VPN Service MVP

Сервис управляет профилями Xray/VLESS Reality через Telegram-бота и веб-админку.

## Основной транспорт

Используется `VLESS + REALITY + XTLS Vision` поверх RAW/TCP на порту `8443`.

- REALITY маскирует TLS-handshake под выбранный обычный HTTPS-сайт.
- XTLS Vision рассчитан на высокую производительность.
- Каждому устройству выдается отдельный UUID.
- Пользователи добавляются в `settings.clients` конфигурации Xray.
- После добавления или удаления клиента контейнер Xray перезапускается.

Абсолютно неблокируемых протоколов не существует. Для публичного сервиса нужно
использовать несколько узлов у разных хостеров, следить за доступностью из разных сетей
и быть готовым менять адреса и транспорт.

## Возможности

- Telegram-бот выдает стандартные `vless://` ссылки;
- до 10 активных устройств на пользователя;
- пробный период начинается при создании первого профиля;
- ручная выдача дней подписки через админку;
- отзыв устройств и блокировка пользователей;
- автоматическое отключение профилей после окончания доступа;
- статистика активности и трафика каждого UUID;
- веб-админка на `/admin`.

Профили импортируются в Hiddify, v2rayN, Nekoray, Happ и другие клиенты с поддержкой
VLESS Reality.

## Архитектура

- `xray`: VLESS Reality сервер на TCP/8443 и локальный API статистики;
- `bot`: Telegram-интерфейс и минутная синхронизация подписок;
- `web`: FastAPI-админка;
- `db`: PostgreSQL.

## Локальный запуск

```bash
cp .env.example .env
# Оставьте VPN_BACKEND=mock и заполните BOT_TOKEN
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
uvicorn app.main:app --reload
python -m app.bot
```

Админка: `http://127.0.0.1:8000/admin`.

## Развертывание на пробном сервере

Нужен Ubuntu/Debian-сервер с публичным IPv4, Docker Compose и свободным TCP-портом
`8443`. Домен для REALITY не обязателен: клиент может подключаться напрямую по IP.

```bash
cp .env.example .env
chmod +x deploy/setup-xray.sh
./deploy/setup-xray.sh
```

Скрипт создаст:

- `deploy/xray/config.json` с приватным ключом REALITY;
- `deploy/xray/reality.env` с публичными параметрами для приложения.

Перенесите значения из `deploy/xray/reality.env` в `.env` и заполните:

```dotenv
BOT_TOKEN=...
ADMIN_PASSWORD=длинный-случайный-пароль
XRAY_PUBLIC_HOST=публичный-IP-сервера
```

Запуск:

```bash
docker compose up -d --build
curl http://127.0.0.1:8000/health
docker compose logs -f xray bot
```

В firewall должен быть открыт `TCP/8443`. Не открывайте наружу Xray API `10085`,
PostgreSQL или админку без HTTPS и ограничения доступа.

Если `deploy/xray/config.json` уже был создан старой версией проекта, установите в
inbound `vless-reality` значение `"port": 8443`, оставьте `serverNames` со значением
`www.microsoft.com` и удалите `HandlerService` из списка `api.services`. Существующий
массив `settings.clients` сохраняйте: приложение продолжит управлять им.

## Управление клиентами Xray

Для MVP приложение не использует `HandlerService/AlterInbound`. При создании устройства
оно добавляет объект клиента в:

```json
{
  "id": "UUID устройства",
  "email": "уникальный ID статистики",
  "flow": "xtls-rprx-vision"
}
```

Путь в конфигурации:

```text
inbounds[tag=vless-reality].settings.clients
```

Изменение записывается атомарно под файловой блокировкой. Если список клиентов
действительно изменился, приложение перезапускает контейнер `vpn-xray` через локальный
Docker socket. Повторная синхронизация существующего клиента не вызывает перезапуск.

Проверить список клиентов:

```bash
docker compose exec bot python -c \
  'import json; c=json.load(open("/data/xray/config.json")); print(next(i for i in c["inbounds"] if i["tag"]=="vless-reality")["settings"]["clients"])'
```

Проверить конфигурацию и выполнить ручной перезапуск:

```bash
docker compose run --rm xray run -test -config /etc/xray/config.json
docker restart vpn-xray
```

`bot` и `web` имеют доступ к `/var/run/docker.sock`, поэтому админку необходимо закрыть
от публичного доступа и защитить сильным паролем. Для следующей версии управление
конфигом и перезапуск следует вынести в отдельный минимальный node-agent.

## Выбор REALITY target

По умолчанию используется `www.microsoft.com:443`. Перед эксплуатацией target нужно
проверить с сервера и выбрать стабильный HTTPS-сайт, желательно в том же ASN или близком
сетевом окружении. Target не должен резолвиться в IP самого VPN-сервера.

Изменить target при генерации:

```bash
REALITY_TARGET=example.com:443 REALITY_SERVER_NAME=example.com ./deploy/setup-xray.sh
```

## Следующие шаги перед публичным запуском

- добавить несколько Xray-узлов и автоматическое переключение;
- добавить запасной транспорт, например VLESS XHTTP/REALITY;
- провести тесты скорости и доступности на российских мобильных и домашних операторах;
- добавить Alembic, резервные копии PostgreSQL и мониторинг;
- поставить HTTPS reverse proxy перед админкой и ограничить доступ.

Если ранее запускалась WireGuard-версия этого MVP, используйте новую пустую базу данных:
схема устройств была изменена с WireGuard-ключей на VLESS UUID.
