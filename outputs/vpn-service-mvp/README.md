# VLESS Reality VPN Service MVP

Сервис управляет профилями Xray/VLESS Reality через Telegram-бота и веб-админку.

## Основной транспорт

Используется `VLESS + REALITY + XTLS Vision` поверх RAW/TCP на порту `443`.

- REALITY маскирует TLS-handshake под выбранный обычный HTTPS-сайт.
- XTLS Vision рассчитан на высокую производительность.
- Каждому устройству выдается отдельный UUID.
- Пользователи добавляются и удаляются без перезапуска Xray через локальный gRPC API.

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

- `xray`: VLESS Reality сервер на TCP/443 и локальный API на `127.0.0.1:10085`;
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
`443`. Домен для REALITY не обязателен: клиент может подключаться напрямую по IP.

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

В firewall должен быть открыт `TCP/443`. Не открывайте наружу Xray API `10085`,
PostgreSQL или админку без HTTPS и ограничения доступа.

## Проверка Xray API через grpcurl

Xray использует собственный `xray.common.serial.TypedMessage`, а не
`google.protobuf.Any`. Поэтому `AlterInboundRequest.operation` должен содержать поля
`type` и `value`, где `value` — base64-сериализованный protobuf операции.

Проверить доступность API и схему:

```bash
docker compose exec bot grpcurl -plaintext \
  127.0.0.1:10085 \
  describe xray.app.proxyman.command.HandlerService.AlterInbound
```

Добавить тестового VLESS-пользователя:

```bash
docker compose exec -T bot python -c \
  'import json; from app.vpn import ADD_USER_OPERATION,add_user_operation_value,alter_inbound_payload; print(json.dumps(alter_inbound_payload("vless-reality",ADD_USER_OPERATION,add_user_operation_value("11111111-1111-1111-1111-111111111111","grpcurl-test@vpn.local"))))' \
| docker compose exec -T bot grpcurl -plaintext -d @ \
  127.0.0.1:10085 xray.app.proxyman.command.HandlerService/AlterInbound
```

Удалить тестового пользователя:

```bash
docker compose exec -T bot python -c \
  'import json; from app.vpn import REMOVE_USER_OPERATION,remove_user_operation_value,alter_inbound_payload; print(json.dumps(alter_inbound_payload("vless-reality",REMOVE_USER_OPERATION,remove_user_operation_value("grpcurl-test@vpn.local"))))' \
| docker compose exec -T bot grpcurl -plaintext -d @ \
  127.0.0.1:10085 xray.app.proxyman.command.HandlerService/AlterInbound
```

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
