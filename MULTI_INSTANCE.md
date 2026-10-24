# Второй экземпляр приложения

В `docker-compose.yml` добавлен необязательный сервис
`strava-noshoes-secondary`. Он использует отдельные:

- ключи Strava API;
- порт `8001` на хосте;
- серверные каталоги `/home/NoShoes2/data` и `/home/NoShoes2/logs`;
- файлы обновлённых Strava-токенов и состояния Telegram-бота.

Основной экземпляр продолжает использовать существующие серверные каталоги:

- `/home/NoShoes/data`;
- `/home/NoShoes/logs`.

## Настройка

Создайте рабочий файл окружения из примера:

```bash
cp .env.secondary.example .env.secondary
```

В PowerShell:

```powershell
Copy-Item .env.secondary.example .env.secondary
```

Заполните в `.env.secondary` как минимум:

```env
STRAVA_CLIENT_ID=...
STRAVA_CLIENT_SECRET=...
STRAVA_ACCESS_TOKEN=...
STRAVA_REFRESH_TOKEN=...
```

Новые `CLIENT_ID` и `CLIENT_SECRET` сами по себе не выбирают другого спортсмена.
Пара `ACCESS_TOKEN`/`REFRESH_TOKEN` должна быть получена через OAuth при входе
именно во второй Strava-аккаунт. Если перенести токены первого аккаунта, Strava
будет возвращать его активности и во втором контейнере.

Для общего Telegram-бота токен задаётся только основному экземпляру.
Polling второго экземпляра отключён через `BOT_ENABLED: "false"` в Compose;
его API и синхронизация Strava продолжают работать.

## Общий бот для двух пользователей

В `.env` основного экземпляра укажите токен выбранного общего бота и два
разных числовых Telegram user ID:

```env
BOT_ENABLED=true
BOT_API_TOKEN=...
BOT_PRIMARY_USER_ID=123456789
BOT_SECONDARY_USER_ID=987654321
BOT_TIMEZONE=Europe/Moscow
```

Замените ID примера своими. При двух нулевых ID сохраняется старый режим
одного аккаунта. Неполная или одинаковая пара ID отклоняется при загрузке
настроек. `BOT_SECONDARY_API_URL` задан в Compose как
`http://strava-noshoes-secondary:8000`: это внутренний адрес второго сервиса,
не внешний порт `8001`. Общий бот работает внутри основного контейнера.

Первый пользователь получает напоминания проверить Gear в Strava по сегодняшним
тренировкам Garmin. Второй получает общее напоминание по сегодняшним тренировкам
Strava: «Сегодня у вас была активность. Проверьте в Strava, правильно ли указана
экипировка». Проверяется текущий календарный день в часовом поясе бота.

Уведомление приходит независимо от выбранного Gear, в том числе при его отсутствии.
При смене кроссовок или экипировки по умолчанию настройки бота менять не нужно.
В дни без тренировок и при одних силовых бот молчит. По расписанию отправляется
одно общее напоминание; `/check` запускает такую же проверку за сегодня и сообщает,
если подходящих тренировок нет. Расписания пользователей остаются отдельными.

Старую переменную `BOT_SECONDARY_DEFAULT_GEAR_BY_SPORT`, если она уже была добавлена
в основной `.env`, можно удалить: она принимается для совместимости, но не влияет
на уведомления. Дополнительных настроек для общего напоминания нет.

Команды `/check`, `/gpx`, обработка ссылок и кнопки GPX выбирают аккаунт
по user ID. В общем режиме бот работает с привязанными ID в личных чатах.
Команда `/id` доступна без привязки и показывает только ID отправителя.

У каждого пользователя собственное расписание и состояние в `users` файла
`BOT_STATE_FILE` основного контейнера. `/start` другого пользователя и его
`/stop` не меняют вашу подписку. Старое состояние переносится, если сохранённый
chat ID совпадает с одним из привязанных пользователей.

После обновления исходников и заполнения `.env` пересоздайте оба сервиса,
чтобы отключить старый polling Secondary и запустить общий бот:

```bash
docker compose --profile secondary up -d --no-deps --build --force-recreate strava-noshoes-secondary strava-noshoes
```

Каждый пользователь должен написать выбранному общему боту `/start`, затем
задать своё расписание, например `/schedule 21:00`. Время общего бота —
московское. `/check` позволяет проверить свою привязку сразу.

## Запуск

Второй экземпляр остаётся на Strava постоянно. В его `environment` в Compose
заданы `MIGRATION_ENABLED: "false"` и `GARMIN_SYNC_ENABLED: "false"`;
такие же значения есть в `.env.secondary.example`. Это сохраняет live Strava
для web API, прогноза, GPX и бота и продолжает ежедневный бэкап в SQLite
после `MIGRATION_CUTOFF`. Просто отключить `GARMIN_SYNC_ENABLED` недостаточно:
без отключения миграции источник всё равно переключится на Garmin.

Только второй экземпляр:

```bash
docker compose --profile secondary up -d --build strava-noshoes-secondary
```

После обновления исходников и Compose на сервере пересоздайте только второй
экземпляр, чтобы применить режим Strava и запустить догрузку при старте
(`STRAVA_BACKUP_ENABLED=true`, `STRAVA_BACKUP_ON_STARTUP=true` в
`.env.secondary`):

```bash
docker compose --profile secondary up -d --no-deps --build --force-recreate strava-noshoes-secondary
curl http://localhost:8001/api/v1/backup/status
```

Ожидаемые поля статуса: `migration_enabled=false`, `current_source="strava"`,
`current_source_enabled=true`, `active_through=null`. Во время догрузки
`running=true`; после завершения проверьте `last_error=null` и дату
`database.latest_activity_at`. Команда пересоздания второго сервиса не
перезапускает основной контейнер на `8000`.

После запуска:

- основной экземпляр: <http://localhost:8000>;
- второй экземпляр: <http://localhost:8001>.

Проверьте, что контейнеры подключены к разным спортсменам:

```bash
curl http://localhost:8000/api/v1/athlete
curl http://localhost:8001/api/v1/athlete
```

Значения поля `id` должны отличаться. Если они одинаковые, во втором `.env`
используются OAuth-токены первого Strava-аккаунта. Если запросы к разным портам
возвращают полностью одинаковый ответ даже после замены токенов, следует
проверить настройки reverse proxy.

Просмотр состояния и логов:

```bash
docker compose --profile secondary ps
docker compose --profile secondary logs -f strava-noshoes-secondary
```

Остановка только второго экземпляра:

```bash
docker compose --profile secondary stop strava-noshoes-secondary
```

Для второго контейнера явно заданы отдельные имена SQLite-базы, файла
Strava-токенов, состояния Telegram-бота и каталога GPX. Даже если содержимое
`/home/NoShoes/data` когда-то копировалось в `/home/NoShoes2/data`, старые файлы
первого экземпляра не будут выбраны вторым контейнером.
