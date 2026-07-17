# Второй экземпляр приложения

В `docker-compose.yml` добавлен необязательный сервис
`strava-noshoes-secondary`. Он использует отдельные:

- ключи Strava API и Telegram;
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
