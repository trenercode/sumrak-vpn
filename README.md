# VLESS Reality VPN Service MVP

Сервис управляет профилями Xray/VLESS Reality через Telegram-бота и веб-админку.

## Основной транспорт

Основной и рекомендуемый режим — `VLESS + REALITY + XHTTP`. Резервный режим —
`VLESS + REALITY + XTLS Vision` поверх RAW/TCP.

- REALITY маскирует TLS-handshake под выбранный обычный HTTPS-сайт.
- XHTTP используется как основной стабильный режим.
- XTLS Vision сохраняется как быстрый резервный режим.
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
- выбор платформы и инструкции по установке из админки;
- реферальная программа: скидка другу и +15 дней после первой оплаты;
- уведомления об окончании trial и подписки без повторного спама;
- рассылки всем, активным, истекающим или одному пользователю;
- веб-админка на `/admin`.
- мобильный Telegram Web App на `/webapp`.

Профили импортируются в Hiddify, v2rayN, Nekoray, Happ и другие клиенты с поддержкой
VLESS Reality.

## Архитектура

- `xray`: VLESS Reality сервер на TCP/8443 и локальный API статистики;
- `bot`: Telegram-интерфейс и минутная синхронизация подписок;
- `web`: FastAPI-админка;
- `db`: PostgreSQL.

Система поддерживает несколько VPN-нод. Каждое устройство имеет отдельный профиль на
каждом активном сервере и одну subscription URL вида `/sub/{token}`. Клиент получает
список стран из этой подписки и может переключаться между ними.

После миграции существующие устройства сохраняют свои UUID и прямые VLESS-профили.
Default-сервер и записи `device_server_profiles` для них создаются автоматически при
первой фоновой синхронизации.

## Локальный запуск

```bash
cp .env.example .env
# Оставьте VPN_BACKEND=mock и заполните BOT_TOKEN, BOT_USERNAME
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
alembic upgrade head
uvicorn app.main:app --reload
python -m app.bot
```

Админка: `http://127.0.0.1:8000/admin`.

## Staging deployment

Staging запускается отдельным Compose-стеком из `/opt/sumrak-vpn-test` и не использует
production `.env`, базу, Xray-контейнер, конфиг или REALITY-ключи. Staging Xray слушает
отдельный публичный порт `18443`, использует контейнер `sumrak-vpn-test-xray` и конфиг
`/opt/sumrak-vpn-test/deploy/xray/config.json`.

```bash
cd /opt/sumrak-vpn-test
git checkout feature/...
cp .env.staging.example .env
# Заполните отдельные BOT_TOKEN, BOT_USERNAME и ADMIN_PASSWORD
STAGING_PUBLIC_HOST=test.sumrak.digital ./deploy/setup-staging-xray.sh
docker compose -f compose.staging.yaml up -d --build
docker compose -f compose.staging.yaml ps
curl http://127.0.0.1:8001/health
```

До запуска откройте входящий TCP-порт `18443` в firewall/security group staging-сервера.
Проверка из внешней сети:

```bash
nc -vz test.sumrak.digital 18443
```

Изолированные ресурсы staging:

- web: `sumrak-vpn-test-web`, локальный порт `127.0.0.1:8001`;
- bot: `sumrak-vpn-test-bot`, использует только отдельный test bot token;
- xray: `sumrak-vpn-test-xray`, публичный порт `18443`;
- db: `sumrak-vpn-test-db`, БД `vpn_test`, локальный порт `127.0.0.1:5433`;
- migrate: `sumrak-vpn-test-migrate`;
- one-shot sync: `sumrak-vpn-test-xray-sync`;
- volume: `sumrak-vpn-test-postgres-data`;
- network: `sumrak-vpn-test-network`.

Настройте reverse proxy для `test.sumrak.digital` на `http://127.0.0.1:8001` и выпустите
отдельный TLS-сертификат. Production `panel.sumrak.digital` продолжает проксироваться на
порт `8000`.

`setup-staging-xray.sh` один раз создаёт отдельные `privateKey`, `publicKey` и `shortId`.
Секретный ключ сохраняется только в staging `config.json`, а публичные параметры и имя
контейнера записываются в `deploy/xray/reality.env`. Скрипт отказывается перезаписывать
существующие ключи и использовать production-порт `8443`.

Перед каждым запуском убедитесь, что `/opt/sumrak-vpn-test/.env` содержит отдельный
тестовый `BOT_TOKEN`. Не копируйте `/opt/sumrak-vpn/.env`, `deploy/xray/config.json` или
`deploy/xray/reality.env` из production и не запускайте staging через production
`compose.yaml`.

Управление стеком:

```bash
docker compose -f compose.staging.yaml logs -f web bot xray
docker compose -f compose.staging.yaml exec xray xray run -test -config /etc/xray/config.json
docker compose -f compose.staging.yaml down
# Удаляет только staging-БД; используйте лишь когда тестовые данные больше не нужны:
docker compose -f compose.staging.yaml down -v
```

При первом запуске default-сервер автоматически создаётся как `local_config` из
staging `XRAY_*` параметров. Одноразовый сервис `staging_xray_sync` также переводит
существующий default-сервер из прежнего `manual/mock` режима в `local_config` и добавляет
в staging Xray уже существующие активные UUID. Сервис отказывается работать с именем
контейнера, отличным от `sumrak-vpn-test-xray`, или с портом `8443`.

В `/admin/servers` проверьте:

- public host: `test.sumrak.digital`;
- public port: `18443`;
- management mode: `local_config`;
- health: `online`.

Переключение Vision/XHTTP через админку изменяет только staging `config.json`, проверяет
его внутри `sumrak-vpn-test-xray`, перезапускает только этот контейнер и выполняет
rollback при ошибке. Docker socket монтируется в staging web/bot исключительно для этого
управления; имя контейнера жёстко задано в staging `reality.env`.

## Telegram Web App

Главное меню бота содержит кнопку «🌑 Открыть Sumrak VPN». Она открывает мобильный
интерфейс с подпиской, устройствами, инструкциями, реферальной программой и поддержкой.
Web App использует существующие сервисы и subscription URL, поэтому не меняет Xray,
админку или логику бота.

Для Telegram нужен публичный HTTPS URL:

```dotenv
PANEL_PUBLIC_URL=https://panel.sumrak.digital
WEBAPP_URL=https://panel.sumrak.digital/webapp
```

API `/api/webapp/*` проверяет подпись Telegram `initData`. Для локальной разработки
можно задать `WEBAPP_DEV_TELEGRAM_ID` и передавать такой же `X-Dev-Telegram-Id`;
на рабочем сервере эту настройку оставляйте пустой.

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
BOT_USERNAME=имя_бота_без_собаки
SUPPORT_TELEGRAM_URL=https://t.me/support_username
PANEL_PUBLIC_URL=https://panel.sumrak.digital
ADMIN_PASSWORD=длинный-случайный-пароль
XRAY_PUBLIC_HOST=публичный-IP-сервера
```

Запуск:

```bash
docker compose up -d --build
curl http://127.0.0.1:8000/health
docker compose logs -f xray bot
```

Перед запуском `web` и `bot` Compose автоматически выполняет `alembic upgrade head`
через одноразовый сервис `migrate`.

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

## VPN-серверы

Раздел `/admin/servers` управляет распределенными VPN-нодами. Первый default-сервер
автоматически создается из текущих `XRAY_*` параметров, поэтому установка с одной нодой
продолжает работать без ручной настройки.

Режимы управления:

- `local_config`: приложение изменяет локальный `config.json` и перезапускает `vpn-xray`;
- `remote_config`: панель по SSH полностью синхронизирует active clients, проверяет
  candidate-конфиг, применяет его и перезапускает удалённый Docker Compose;
- `manual`: приложение создает UUID, URI и запись профиля, но администратор вручную
  добавляет UUID/email в конфиг удаленной ноды;
- `ssh_future` является совместимым alias для `remote_config`, `agent_future`
  зарезервирован под будущую автоматизацию.

Для manual-сервера активные UUID и email видны на странице сервера. После их добавления
в Xray config ноду нужно перезапустить вручную.

### Remote config / SSH

Для рабочих удалённых нод рекомендуется `management_mode=remote_config`. Контейнеры
`web` и `bot` должны иметь read-only доступ к отдельному SSH private key, путь внутри
контейнера указывается в `ssh_key_path`. Добавьте fingerprint удалённой ноды в
`known_hosts`: SSH запускается с `BatchMode=yes` и `StrictHostKeyChecking=yes`.

При каждой синхронизации панель:

1. читает текущий remote `config.json`, сохраняя его REALITY private key и остальные
   настройки;
2. собирает `settings.clients` из всех активных `device_server_profiles` сервера;
3. загружает `config.candidate.json`;
4. проверяет candidate через отдельный `ghcr.io/xtls/xray-core:latest`;
5. создаёт `config.json.backup`, применяет candidate и выполняет `docker compose restart`;
6. при ошибке возвращает backup. Невалидный candidate остаётся для диагностики.

Пример France:

```text
public_host: 31.56.146.138
public_port: 443
transport: xhttp
management_mode: remote_config
remote_xray_config_path: /opt/xray-fr/config.json
remote_compose_dir: /opt/xray-fr
remote_container_name: xray-fr
ssh_host: 31.56.146.138
ssh_port: 22
ssh_user: root
ssh_key_path: /run/secrets/france_xray_key
```

На удалённой ноде должны быть установлены Docker и Docker Compose, а SSH-пользователь
должен иметь права читать/писать конфиг и управлять Compose-проектом.

Сервер можно удалить только при отсутствии активных профилей. Выключенный сервер
исчезает из subscription URL, но существующая Xray-нода и уже выданные прямые URI не
останавливаются.

### Subscription URL

```text
GET https://panel.sumrak.digital/sub/{device_token}
GET https://panel.sumrak.digital/sub/{device_token}?base64=true
```

Обычный ответ содержит один VLESS URI на строку. Вариант `base64=true` возвращает
base64-compatible список. Новые устройства получают ссылку подписки в Telegram.

### Health check

Bot worker каждые пять минут делает TCP connect к `public_host:public_port` каждой
активной ноды. Статус `online/offline/error` отображается в админке. Проверку также
можно запустить вручную со страницы сервера.

## Что будет, если центральный сервер недоступен

Xray-ноды работают независимо от Telegram-бота, админки и PostgreSQL. Уже добавленные
профили продолжат подключаться и передавать трафик. В период недоступности центрального
сервера нельзя создавать новые устройства, обновлять subscription URL, проводить
оплаты или менять конфиги нод. После восстановления управление продолжит работу.

Будущий `sumrak-node-agent` подключится через интерфейс `NodeManager`; заготовки
`LocalConfigNodeManager`, `ManualNodeManager` и `AgentNodeManager` уже разделяют способы
управления нодами.

### Vision и XHTTP

В `/admin/servers` транспорт переключается между:

- **Стабильный режим (рекомендуется): XHTTP**: Xray inbound получает `network: xhttp` и
  `xhttpSettings` (`path` и `mode`), а `flow` удаляется из клиентов и subscription URI.
- **Быстрый режим (резервный): Vision**: Xray inbound получает актуальный RAW-транспорт
  (`network: raw`), клиенты получают `flow: xtls-rprx-vision`, а subscription URI
  содержит совместимый клиентский параметр `type=tcp`;

Новые серверы создаются с `transport=xhttp`, пустым `flow`, `path=/` и `mode=auto`.
Существующие Vision-серверы миграция не переключает автоматически.

Для `local_config` приложение не заменяет рабочий конфиг сразу. Оно создаёт рядом
`config.candidate.json`, запускает в контейнере:

```bash
xray run -test -config /etc/xray/config.candidate.json
```

После успешной проверки текущий файл сохраняется как `config.json.backup`, candidate
применяется и контейнер перезапускается. Если контейнер не запустился, приложение
возвращает backup и повторно запускает Xray. Список `settings.clients`, UUID и закрытый
REALITY-ключ при переключении транспорта сохраняются.

Успешность `xray run -test` определяется только по exit code команды. stdout/stderr
показывается как обычный текст. При ошибке `config.candidate.json` остаётся рядом с
рабочим конфигом для диагностики и его путь выводится в сообщении админки.

### Staging-проверка XHTTP

Переключение сначала проверяйте только на `test.sumrak.digital` с отдельными БД,
Telegram-ботом, Xray-контейнером и Reality-ключами. Не подключайте production
`deploy/xray/config.json` к staging-контейнерам.

1. Запустите изолированный staging через `compose.staging.yaml`.
2. В `/admin/servers` добавьте ноду с `management_mode=manual` и транспортом XHTTP,
   проверьте subscription URI без изменения Xray.
3. Поднимите отдельный staging Xray и добавьте ноду `local_config`, указав путь к его
   config. Переключите её в «Стабильный режим: XHTTP».
4. Убедитесь, что админка показала успешное сохранение, `config.json.backup` создан,
   а staging Xray работает.
5. Создайте новое устройство через staging Telegram-бота и проверьте импорт subscription
   URL в Hiddify, v2rayN, Nekoray и Happ.
6. Проверьте Vision и XHTTP на Instagram/Reels/YouTube, затем отдельно проверьте rollback
   заведомо невалидного staging-конфига.

Автоматические тесты проверяют структуру Vision/XHTTP URI, сохранение клиентов и rollback.
Импорт и качество трафика в конкретных клиентах остаются обязательным ручным staging-тестом.

## Техподдержка

Укажите публичную Telegram-ссылку:

```dotenv
SUPPORT_TELEGRAM_URL=https://t.me/username
```

Бот показывает inline-кнопку поддержки в главном меню, инструкциях и важных ошибках.

## VPN-клиенты и инструкции

Раздел админки `/admin/clients` управляет приложениями, которые бот рекомендует
пользователям. Для каждой платформы задаются название, ссылка скачивания, инструкция,
порядок сортировки и активность. Бот показывает только активные записи.

При создании профиля пользователь сначала выбирает платформу. Она сохраняется в
`devices.platform`, а устройство получает понятное имя: `iPhone 1`, `Android 1` и т.д.
Профиль выдается только как VLESS-ссылка в отдельном code-блоке, без файла.

## Реферальная программа

Персональная ссылка имеет вид:

```text
https://t.me/BOT_USERNAME?start=ref_REFERRAL_CODE
```

При регистрации пригласивший фиксируется один раз и больше не меняется. Первая успешная
оплата должна вызывать сервисный hook:

```python
await record_successful_payment(session, user, subscription_days=30)
```

Он отмечает первую оплату и скидку приглашенного, начисляет пригласившему +15 дней и
создает запись в `referral_rewards`. До подключения платежки сценарий можно проверить
кнопкой «Зафиксировать оплату» на странице пользователя в админке. Простая ручная выдача
дней не считается оплатой.

## Уведомления

Bot worker каждые пять минут проверяет:

- trial: за 24 часа и после окончания;
- подписку: за 3 дня, за 1 день и после окончания;
- отключенные после окончания доступа устройства;
- начисленные реферальные бонусы.

Каждое отправленное событие записывается в `notification_log`; ключ включает дату
окончания, поэтому после продления уведомления рассчитываются заново и не дублируются.

## Рассылки

Раздел `/admin/broadcasts` позволяет создать черновик, увидеть preview и подтвердить
отправку. Поддерживаются текст и Telegram `file_id`/URL картинки. Доступные аудитории:
все пользователи, активные, с истекающей подпиской и один пользователь.

После подтверждения создаются `broadcast_recipients`. Bot worker отправляет сообщения с
задержкой `BROADCAST_DELAY_SECONDS`, записывает успешные отправки и ошибки и продолжает
работу, если пользователь заблокировал бота.

## Миграции БД

Схема управляется Alembic:

```bash
docker compose run --rm migrate
# или локально:
alembic upgrade head
```

Миграция добавляет реферальные поля, `devices.platform`, `notification_log`,
`vpn_clients`, `broadcasts` и `broadcast_recipients`. Также она снимает `NOT NULL` со
старых WireGuard-полей `public_key` и `assigned_ip`, если они сохранились в базе.

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
- добавить дополнительные транспорты;
- провести тесты скорости и доступности на российских мобильных и домашних операторах;
- добавить резервные копии PostgreSQL и мониторинг;
- поставить HTTPS reverse proxy перед админкой и ограничить доступ.

Перед обновлением production-базы сделайте резервную копию PostgreSQL и примените
`alembic upgrade head`.
