# Второй экземпляр приложения

В `docker-compose.yml` добавлен необязательный сервис
`strava-noshoes-secondary`. Он использует отдельные:

- ключи Strava API и Telegram;
- порт `8001` на хосте;
- каталоги `data-secondary/` и `logs-secondary/`;
- файлы обновлённых Strava-токенов и состояния Telegram-бота.

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

Если нужен Telegram-бот, укажите отдельный `BOT_API_TOKEN`. Один Telegram-токен
нельзя одновременно использовать в двух контейнерах с polling.

## Запуск

Только второй экземпляр:

```bash
docker compose --profile secondary up -d --build strava-noshoes-secondary
```

После запуска:

- основной экземпляр: <http://localhost:8000>;
- второй экземпляр: <http://localhost:8001>.

Просмотр состояния и логов:

```bash
docker compose --profile secondary ps
docker compose --profile secondary logs -f strava-noshoes-secondary
```

Остановка только второго экземпляра:

```bash
docker compose --profile secondary stop strava-noshoes-secondary
```
