# Резервирование Strava и переход на Garmin

## Поведение приложения

- Основной экземпляр (`8000`) использует переход по дате с
  `MIGRATION_ENABLED=true` (значение по умолчанию). Второй экземпляр (`8001`)
  работает только со Strava: `MIGRATION_ENABLED=false`, без даты отключения
  Strava и без автоматических запросов к Garmin.
- До конца `2026-10-15` web API, прогноз и Telegram-бот читают live Strava.
- При старте контейнера и затем ежедневно в `04:15` все summary-данные Strava
  идемпотентно обновляются в SQLite.
- GPS/HR/cadence streams догружаются порциями по 60 активностей за запуск. Это
  оставляет запас относительно стандартного read-лимита Strava в 100 запросов
  за 15 минут. Уже сохранённые streams повторно не скачиваются.
- С `2026-10-16` история до cutoff читается из SQLite, новые активности — из
  Garmin Connect и ежедневно сохраняются в ту же SQLite. При временной
  недоступности Garmin приложение использует последнюю синхронизированную копию.
- Проверка Gear использует activity-specific Garmin endpoint. Силовые тренировки
  (`WeightTraining`) исключены из проверки и напоминаний о Gear.

## Напоминания основного Telegram-бота

После перехода на Garmin бот напоминает проверить Gear в Strava, если за
текущий календарный день есть тренировки Garmin, кроме силовых. Gear в Garmin
не проверяется и не влияет на напоминание: бот не утверждает, что Gear в Strava
отсутствует. В дни без подходящих тренировок сообщение по расписанию не приходит.
Ручная команда `/check` использует ту же логику за сегодня.

Основному контейнеру в Compose задан `BOT_TIMEZONE=Europe/Moscow`. После
обновления отправьте его боту `/schedule 21:00`, чтобы установить ежедневное
напоминание на 21:00 по Москве. Время сохраняется в существующем файле состояния
бота. У второго аккаунта остаётся прежняя проверка отсутствующего Gear в Strava
и отдельное расписание. Эти два аккаунта можно обслуживать одним ботом по
`BOT_PRIMARY_USER_ID` и `BOT_SECONDARY_USER_ID`; настройка описана в
`MULTI_INSTANCE.md`. В таком режиме polling запускается только в основном
контейнере, а второй аккаунт читается через API Secondary.

Настройки основного экземпляра в `.env`:

```env
MIGRATION_ENABLED=true
MIGRATION_CUTOFF=2026-10-15
STRAVA_BACKUP_ENABLED=true
STRAVA_BACKUP_ON_STARTUP=true
STRAVA_BACKUP_SCHEDULE_HOUR=4
STRAVA_BACKUP_SCHEDULE_MINUTE=15
STRAVA_BACKUP_STREAMS_PER_RUN=60
GARMIN_SYNC_ENABLED=true
GARMIN_SYNC_LOOKBACK_DAYS=7

GARMIN_EMAIL=...
GARMIN_PASSWORD=...
GARMIN_TOKEN_STORE=/app/data/garmin_tokens
```

Второй контейнер использует собственные Strava credentials и БД.
Его Compose-конфигурация фиксирует `MIGRATION_ENABLED=false` и
`GARMIN_SYNC_ENABLED=false`. Эти значения из `environment` имеют приоритет
над `.env.secondary`. Подробности применения — в `MULTI_INSTANCE.md`.

После перехода основного экземпляра на Garmin в его `.env` можно задать:

```env
STRAVA_BACKUP_ENABLED=false
STRAVA_BACKUP_ON_STARTUP=true
GARMIN_SYNC_ENABLED=true
```

`STRAVA_BACKUP_ENABLED` управляет только бэкапом Strava и после переключения
не влияет на Garmin и чтение сохранённой истории. Несмотря на название,
`STRAVA_BACKUP_ON_STARTUP` запускает синхронизацию текущего источника:
до переключения Strava, после — Garmin. При `false` синхронизация при старте
пропускается, ежедневный запуск по расписанию остаётся включённым через
`GARMIN_SYNC_ENABLED=true`. Для второго экземпляра, который остаётся на Strava,
оба флага `STRAVA_BACKUP_*` оставьте включёнными.

## Проверка после деплоя

Оба endpoint безопасны и не содержат токенов:

```text
http://5.181.187.148:8000/api/v1/backup/status
http://5.181.187.148:8001/api/v1/backup/status
```

Поле `database.activities` должно совпасть с количеством доступных тренировок
Strava. `activities_pending_streams` будет уменьшаться после каждого запуска и
в итоге станет равным нулю. `last_error` должен быть `null`.

После переключения основного экземпляра поле `current_source` станет `garmin`,
а рост синхронизации
будет виден по `database.garmin_activities` и
`database.latest_garmin_activity_at`.

На `8001` поле `current_source` остаётся `strava`, `active_through=null`,
а догрузка видна по `database.activities` и `database.latest_activity_at`.

SQLite-файлы находятся в постоянных host volumes:

- `/home/NoShoes/data/strava_noshoes.db`;
- `/home/NoShoes2/data/strava_noshoes-secondary.db`.

Это защищает данные при пересоздании контейнера, но не при потере диска сервера.
Для полноценной резервной копии оба файла нужно включить во внешний snapshot или
регулярное копирование на другой хост/объектное хранилище.

## Простая ручная выгрузка SQLite

Не копируйте рабочий `.db` напрямую во время записи. Сначала создайте согласованный
snapshot штатным SQLite Backup API внутри каждого контейнера:

```bash
docker exec strava-noshoes-app python -c "import sqlite3; s=sqlite3.connect('/app/data/strava_noshoes.db'); d=sqlite3.connect('/app/data/strava_noshoes.snapshot.db'); s.backup(d); d.close(); s.close()"
docker exec strava-noshoes-app-secondary python -c "import sqlite3; s=sqlite3.connect('/app/data/strava_noshoes-secondary.db'); d=sqlite3.connect('/app/data/strava_noshoes-secondary.snapshot.db'); s.backup(d); d.close(); s.close()"
```

Затем скачайте снимки с локального компьютера (подставьте SSH-пользователя):

```bash
scp your-user@5.181.187.148:/home/NoShoes/data/strava_noshoes.snapshot.db .
scp your-user@5.181.187.148:/home/NoShoes2/data/strava_noshoes-secondary.snapshot.db .
```

Для полного восстановления контейнеров отдельно сохраните `.env`,
`.env.secondary`, `strava_tokens*.json`, `bot_state*.json` и каталоги
`garmin_tokens*`. Они содержат секреты, поэтому храните их только в
зашифрованном архиве; для обычного бэкапа тренировок достаточно двух SQLite
snapshot-файлов.

## Ручной запуск

При необходимости backup можно запустить внутри каждого контейнера:

```bash
docker exec strava-noshoes-app python -m scripts.backup_strava
docker exec strava-noshoes-app-secondary python -m scripts.backup_strava
```

После cutoff Garmin-синхронизацию можно запустить вручную:

```bash
docker exec strava-noshoes-app python -m scripts.backup_garmin
```

Одновременный ручной и автоматический запуск для одного контейнера не нужен.
