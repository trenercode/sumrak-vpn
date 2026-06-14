# Sumrak VPN

Sumrak VPN — сервис управления VPN-доступом на базе Telegram-бота, FastAPI,
PostgreSQL и Xray VLESS + REALITY.

Основной режим для новых agent-нод:

- VLESS + REALITY + XHTTP;
- post-quantum VLESS encryption;
- ML-DSA65 REALITY identity;
- Xray Core `26.6.1`.

Резервный режим — VLESS + REALITY + XTLS Vision.

## Возможности

- Telegram-бот и Telegram Web App;
- пробный период и ручная выдача подписок;
- до 10 устройств пользователя;
- subscription URL со списком доступных серверов;
- мультисерверная архитектура;
- автоматическое подключение новых VPN-нод одной командой;
- синхронизация UUID через `sumrak-node-agent`;
- безопасное применение Xray config с проверкой и rollback;
- админка серверов, пользователей, клиентов и рассылок;
- реферальная программа;
- уведомления об окончании доступа;
- PostgreSQL и Alembic-миграции.

Платёжная система пока не подключена. Успешную оплату администратор отмечает вручную.

## Архитектура

Центральный сервер:

- `web` — FastAPI, админка, Web App, subscription и node API;
- `bot` — Telegram-бот, уведомления и рассылки;
- `db` — PostgreSQL;
- `migrate` — применение Alembic-миграций;
- `xray` — локальная/default VPN-нода, если она используется.

Удалённая agent-нода:

- `sumrak-node-xray` — Xray Core;
- `sumrak-node-agent` — получает клиентов от панели и применяет конфиг;
- `/opt/sumrak-node/config.json` — рабочий Xray config;
- `/opt/sumrak-node/config.json.backup` — предыдущий рабочий config.

Xray-ноды продолжают обслуживать уже выданные профили, если центральный сервер,
Telegram-бот или PostgreSQL временно недоступны. Центральный сервер нужен для создания
профилей, обновления подписок и управления доступом.

## Требования

- Linux-сервер;
- Docker Engine;
- Docker Compose plugin;
- домен с HTTPS для центральной панели;
- Telegram-бот от BotFather;
- открытый TCP-порт `443` на удалённых VPN-нодах.

Отдельный домен для каждой VPN-ноды не требуется. Agent автоматически определяет
публичный IP и использует его как `public_host`.

## Настройка `.env`

Создайте `.env`:

```bash
cp .env.example .env
```

Основные параметры:

```env
DATABASE_URL=postgresql+asyncpg://vpn:vpn@127.0.0.1:5432/vpn

BOT_TOKEN=
BOT_USERNAME=
SUPPORT_TELEGRAM_URL=https://t.me/username

PANEL_PUBLIC_URL=https://panel.sumrak.digital
WEBAPP_URL=https://panel.sumrak.digital/webapp

ADMIN_USERNAME=admin
ADMIN_PASSWORD=change-me

TRIAL_DAYS=3
MAX_DEVICES=10
BROADCAST_DELAY_SECONDS=0.08

VPN_BACKEND=xray
```

Обязательно измените пароль PostgreSQL в `compose.yaml` и `DATABASE_URL`, если проект
доступен не только в изолированной тестовой среде.

## Первый запуск центрального сервера

Сгенерируйте конфиг локального Xray:

```bash
./deploy/setup-xray.sh
```

Скрипт создаст:

- `deploy/xray/config.json`;
- `deploy/xray/reality.env`.

Добавьте значения из `deploy/xray/reality.env` в `.env`, затем запустите сервисы:

```bash
docker compose up -d --build
```

Проверка:

```bash
docker compose ps
docker compose logs --tail 100 web
docker compose logs --tail 100 bot
docker compose logs --tail 100 xray
curl http://127.0.0.1:8000/health
```

Админка доступна по адресу:

```text
https://PANEL_PUBLIC_URL/admin
```

FastAPI слушает порт `8000`. Перед ним необходимо настроить HTTPS reverse proxy.

## Миграции БД

Compose автоматически запускает `alembic upgrade head` перед `web` и `bot`.

Ручное применение:

```bash
docker compose run --rm migrate
```

Локально:

```bash
alembic upgrade head
```

Не изменяйте production-схему вручную через SQL. Перед обновлением production сделайте
резервную копию PostgreSQL.

## Telegram-бот

Команды:

- `/start` — старт;
- `/privacy` — политика конфиденциальности;
- `/terms` — пользовательское соглашение.

Главное меню:

- получить профиль;
- мои устройства;
- подписка;
- реферальная программа;
- инструкции по подключению;
- техподдержка.

Бот выдаёт subscription URL:

```text
https://PANEL_PUBLIC_URL/sub/{device_token}
```

Subscription содержит по одному VLESS URI на каждый активный сервер. Также доступен
base64-формат:

```text
GET /sub/{device_token}?base64=true
```

## Telegram Web App

Web App доступен по адресу:

```text
https://PANEL_PUBLIC_URL/webapp
```

Укажите этот URL как Telegram Menu Button. Для локальной разработки можно использовать
`WEBAPP_DEV_TELEGRAM_ID`, но его нельзя включать в production.

## Подключение новой VPN-ноды

1. Откройте `/admin/servers`.
2. Нажмите «Подключить новую ноду».
3. Укажите название и код страны.
4. Выполните показанную команду от `root` на чистом VPN-сервере:

```bash
curl -sSL https://PANEL_PUBLIC_URL/node/install.sh | bash -s -- NODE_TOKEN
```

Установщик:

1. устанавливает Docker и Compose plugin при необходимости;
2. создаёт `/opt/sumrak-node`;
3. использует Xray Core `26.6.1`;
4. генерирует X25519 REALITY keys и shortId;
5. генерирует VLESS encryption/decryption через `xray vlessenc`;
6. генерирует ML-DSA65 identity через `xray mldsa65`;
7. создаёт рабочий PQ-XHTTP config;
8. запускает Xray и agent;
9. регистрирует ноду в панели;
10. синхронизирует активные UUID.

Agent каждые 30 секунд получает список активных клиентов. При изменении он:

1. создаёт `config.candidate.json`;
2. проверяет его через `xray run -test`;
3. сохраняет backup;
4. применяет config;
5. перезапускает только `sumrak-node-xray`;
6. восстанавливает backup при ошибке.

Проверка после установки:

```bash
docker ps
docker logs --tail 100 sumrak-node-agent
docker logs --tail 100 sumrak-node-xray
docker run --rm \
  -v /opt/sumrak-node/config.json:/etc/xray/config.json:ro \
  ghcr.io/xtls/xray-core:26.6.1 run -test -config /etc/xray/config.json
```

Ожидаемый результат:

- оба контейнера имеют статус `Up`;
- agent пишет `sync completed: clients=N`;
- Xray возвращает `Configuration OK`;
- в админке нода отображается как `online`.

## Повторная установка ноды

Перед повторной установкой очистите старый deployment на самой VPN-ноде. Новая
регистрация автоматически выключает предыдущую активную agent-запись той же физической
машины.

Панель сравнивает не только строки `public_host`, но и IP-адреса, полученные через DNS.
Поэтому записи `franc.sumrak.digital` и `31.56.146.138` распознаются как одна нода.

В subscription URL остаётся только новая активная запись.

## Режимы управления серверами

- `agent` — рекомендуемый режим для новых удалённых нод;
- `local_config` — управление локальным config и контейнером Xray;
- `remote_config` — управление удалённым config по SSH;
- `manual` — панель создаёт URI и UUID, но не управляет Xray config.

`agent_future` и `ssh_future` поддерживаются как совместимые alias.

### Agent

Рекомендуемый режим. Не требует ручного SSH после установки. Agent сообщает:

- `last_seen_at`;
- `last_sync_at`;
- версию;
- количество клиентов;
- последнюю ошибку.

Если agent не обращался к панели больше двух минут, нода отображается как `offline`.

### Local config

Панель изменяет локальный `config.json`, проверяет candidate и перезапускает локальный
контейнер Xray.

### Remote config / SSH

Панель подключается к удалённой машине по SSH, загружает candidate, проверяет его через
`ghcr.io/xtls/xray-core:26.6.1`, применяет config и выполняет rollback при ошибке.

Для этого режима заполните:

- `ssh_host`;
- `ssh_port`;
- `ssh_user`;
- `ssh_key_path`;
- `remote_xray_config_path`;
- `remote_compose_dir`;
- `remote_container_name`.

### Manual

Панель не изменяет удалённый Xray config. UUID и email нужно добавлять вручную.
Используйте этот режим только для диагностики или внешних систем управления.

## PQ-XHTTP

Новые agent-ноды используют:

- `network: xhttp`;
- `security: reality`;
- post-quantum VLESS encryption/decryption;
- `mldsa65Seed` на сервере;
- `pqv` и `spx` в URI;
- XHTTP padding и дополнительные параметры;
- REALITY target `web.max.ru:443`;
- fingerprint `firefox`.

Старые серверы без `pq_enabled` продолжают работать в legacy-режиме и автоматически не
переводятся на PQ-XHTTP.

Vision остаётся резервным транспортом и использует `flow=xtls-rprx-vision`.

## Управление и удаление серверов

Выключенный сервер исчезает из новых subscription URL.

Кнопка «Удалить»:

- удаляет выбранную запись `vpn_servers`;
- удаляет только её `device_server_profiles`;
- не вызывает revoke, sync или restart;
- не обращается к физической VPN-ноде;
- не затрагивает другие online-серверы, даже если они находятся на том же IP.

Это позволяет безопасно удалять старые выключенные записи с историческими профилями.

Перед удалением проверьте, что выбрана нужная запись. Удаление активной записи уберёт её
из subscription URL, но не остановит контейнер Xray на удалённом сервере.

## Health check

Для активных серверов выполняется TCP-проверка `public_host:public_port`.

Для agent-нод дополнительно учитывается время последнего обращения agent:

- `online` — нода доступна и agent регулярно синхронизируется;
- `offline` — порт недоступен или agent давно не обращался;
- `error` — agent сообщил ошибку.

Статус можно проверить вручную со страницы сервера.

## Пользователи и подписки

Админка позволяет:

- выдавать дни подписки;
- отмечать первую успешную оплату;
- блокировать пользователя;
- отзывать устройства;
- смотреть trial, подписку и статистику устройств.

Trial и лимит устройств сохраняются при добавлении новых VPN-серверов.

## Реферальная программа

- пользователь получает персональную ссылку;
- приглашённый сохраняет связь с пригласившим через `/start ref_CODE`;
- пригласивший получает `+15` дней после первой отмеченной оплаты приглашённого;
- повторное начисление блокируется;
- история хранится в PostgreSQL.

## VPN-клиенты и инструкции

Раздел `/admin/clients` управляет рекомендуемыми приложениями и инструкциями:

- платформа;
- название;
- описание;
- download URL;
- текст инструкции;
- порядок;
- активность.

Бот показывает только активные приложения.

## Уведомления

Bot worker отправляет уведомления:

- trial: за 24 часа и после окончания;
- подписка: за 3 дня, за 1 день и после окончания;
- отключение устройств после окончания доступа;
- регистрация и оплата приглашённого пользователя.

Отправленные события записываются в `notification_log`, чтобы избежать дублей.

## Рассылки

Раздел `/admin/broadcasts` поддерживает:

- всех пользователей;
- только активных;
- пользователей с истекающей подпиской;
- одного пользователя;
- текст;
- Telegram `file_id` или URL изображения;
- preview и подтверждение.

Рассылка выполняется с задержкой `BROADCAST_DELAY_SECONDS`. Ошибки отдельных получателей
записываются и не останавливают всю рассылку.

## Staging

Staging использует отдельные:

- PostgreSQL;
- Telegram-бот;
- Xray config и REALITY keys;
- Xray-контейнер;
- порт `18443`;
- Docker network и volume.

Подготовка:

```bash
cp .env.staging.example .env
./deploy/setup-staging-xray.sh
docker compose -f compose.staging.yaml up -d --build
```

Проверка:

```bash
docker compose -f compose.staging.yaml ps
docker compose -f compose.staging.yaml logs --tail 100 web bot xray
```

Не копируйте production-ключи и production-базу в staging.

## Обновление

Центральный сервер:

```bash
git pull
docker compose run --rm migrate
docker compose pull xray
docker compose up -d --build web bot xray
docker compose ps
```

После изменений installer или agent создавайте новую agent-ноду через админку.
Установленные agent-контейнеры самостоятельно не скачивают новую версию `agent.py`.

## Диагностика

Центральный сервер:

```bash
docker compose ps
docker compose logs --tail 200 web
docker compose logs --tail 200 bot
docker compose logs --tail 200 xray
docker compose run --rm migrate
```

Agent-нода:

```bash
cd /opt/sumrak-node
docker compose ps
docker logs --tail 200 sumrak-node-agent
docker logs --tail 200 sumrak-node-xray
ss -lntp | grep ':443 '
```

Проверка Xray config:

```bash
docker run --rm \
  -v /opt/sumrak-node/config.json:/etc/xray/config.json:ro \
  ghcr.io/xtls/xray-core:26.6.1 run -test -config /etc/xray/config.json
```

Если профиль импортируется, но интернет не работает, сначала проверьте:

1. в subscription осталась только одна активная запись физической ноды;
2. agent успешно получил клиентов;
3. UUID профиля присутствует в `/opt/sumrak-node/config.json`;
4. Xray config проходит `run -test`;
5. Xray-логи содержат `accepted ... [vless-reality >> direct]`.

## Telegram MTProto Proxy

Telegram Proxy — отдельный FakeTLS MTProto-прокси только для Telegram. Это не VPN: он не
маршрутизирует браузер, приложения и другой трафик устройства. Proxy-ноды всегда ставятся на
отдельные VPS и не смешиваются с Xray/VPN-нодательной инфраструктурой.

Создание:

1. Откройте `/admin/telegram-proxies`.
2. Нажмите «Создать ссылку установки».
3. В течение 30 минут запустите показанную одноразовую команду от `root` на чистом
   Ubuntu/Debian VPS.
4. Установщик проверит свободный TCP-порт 443 и создаст только
   `/opt/sumrak-telegram-proxy`.
5. После регистрации нода появится `online`, а бот начнёт выдавать её через кнопку
   «🔗 Прокси Telegram».

Установщик не изменяет SSH, Xray, VPN-ноды или другие каталоги. Install token хранится в БД
только как hash, действует 30 минут и после успешной регистрации повторно не используется.

Рабочие ноды используют официальный `telegrammessenger/proxy` с random padding: пользователю
выдаётся secret с префиксом `dd`, рекомендованным официальным MTProxy для защиты от определения
по размерам пакетов. Sponsor tag от `@MTProxybot` передаётся контейнеру через `TAG`.

Проверка и диагностика proxy-ноды:

```bash
ss -lntp | grep ':443 '
cd /opt/sumrak-telegram-proxy
docker compose ps
docker logs --tail 200 sumrak-telegram-proxy-agent
docker logs --tail 200 sumrak-telegram-proxy
```

Для удаления сначала выключите ноду в `/admin/telegram-proxies`, затем удалите запись. Это не
останавливает VPS автоматически; при необходимости отдельно выполните на proxy-ноду:

```bash
cd /opt/sumrak-telegram-proxy
docker compose down
```

## Тесты

Установите dev-зависимости:

```bash
python3 -m pip install -e '.[dev]'
```

Запуск:

```bash
DATABASE_URL=sqlite+aiosqlite:// pytest -q
ruff check app tests alembic
git diff --check
```

## Безопасность

- используйте HTTPS для панели, Web App, subscription и node API;
- установите сильный `ADMIN_PASSWORD`;
- ограничьте доступ к админке;
- не публикуйте `.env`, PostgreSQL-пароли, node token, agent token и REALITY private keys;
- node enrollment token одноразовый и действует 30 минут;
- `/var/run/docker.sock` предоставляет высокий уровень доступа к Docker host;
- регулярно создавайте резервные копии PostgreSQL;
- обновляйте зависимости и Xray только после проверки в staging.
