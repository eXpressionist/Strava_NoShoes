# Резервирование Strava и переход на Garmin

## Поведение приложения

- До конца `2026-10-15` web API, прогноз и Telegram-бот читают live Strava.
- При старте контейнера и затем ежедневно в `04:15` все summary-данные Strava
  идемпотентно обновляются в SQLite.
- GPS/HR/cadence streams догружаются порциями по 60 активностей за запуск. Это
  оставляет запас относительно стандартного read-лимита Strava в 100 запросов
  за 15 минут. Уже сохранённые streams повторно не скачиваются.
- С `2026-10-16` история до cutoff читается из SQLite, новые активности — из
  Garmin Connect. Проверка Gear использует activity-specific Garmin endpoint;
  пустой ответ означает, что Gear не назначен.

Настройки можно переопределить в `.env` и `.env.secondary`:

```env
MIGRATION_CUTOFF=2026-10-15
STRAVA_BACKUP_ENABLED=true
STRAVA_BACKUP_ON_STARTUP=true
STRAVA_BACKUP_SCHEDULE_HOUR=4
STRAVA_BACKUP_SCHEDULE_MINUTE=15
STRAVA_BACKUP_STREAMS_PER_RUN=60

GARMIN_EMAIL=...
GARMIN_PASSWORD=...
GARMIN_TOKEN_STORE=/app/data/garmin_tokens
```

Второй контейнер использует собственные credentials и БД. Для него рекомендуется
`GARMIN_TOKEN_STORE=/app/data/garmin_tokens-secondary`.

## Проверка после деплоя

Оба endpoint безопасны и не содержат токенов:

```text
http://5.181.187.148:8000/api/v1/backup/status
http://5.181.187.148:8001/api/v1/backup/status
```

Поле `database.activities` должно совпасть с количеством доступных тренировок
Strava. `activities_pending_streams` будет уменьшаться после каждого запуска и
в итоге станет равным нулю. `last_error` должен быть `null`.

SQLite-файлы находятся в постоянных host volumes:

- `/home/NoShoes/data/strava_noshoes.db`;
- `/home/NoShoes2/data/strava_noshoes-secondary.db`.

Это защищает данные при пересоздании контейнера, но не при потере диска сервера.
Для полноценной резервной копии оба файла нужно включить во внешний snapshot или
регулярное копирование на другой хост/объектное хранилище.

## Ручной запуск

При необходимости backup можно запустить внутри каждого контейнера:

```bash
docker exec strava-noshoes-app python -m scripts.backup_strava
docker exec strava-noshoes-app-secondary python -m scripts.backup_strava
```

Одновременный ручной и автоматический запуск для одного контейнера не нужен.

