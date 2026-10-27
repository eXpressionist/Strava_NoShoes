# Strava NoShoes 🏃‍♂️

Современное Python-приложение для взаимодействия с Strava API с веб-интерфейсом для управления активностями и загрузки GPX-треков.

## 🚀 Возможности

- **Подключение к Strava API** - Полная интеграция с официальным API Strava
- **Управление активностями** - Просмотр, фильтрация и анализ ваших тренировок
- **Фильтрация по экипировке** - Поиск активностей без указанной экипировки (Gear)
- **Загрузка GPX-треков** - Скачивание треков в формате GPX
- **Веб-интерфейс** - Современный responsive интерфейс на Bootstrap
- **REST API** - Полнофункциональный API с документацией
- **Асинхронная архитектура** - Высокая производительность благодаря async/await
- **Docker поддержка** - Легкое развертывание через Docker и Docker Compose

## 🏗️ Архитектура

Проект построен по современным принципам с разделением на слои:

```
app/
├── api/           # FastAPI маршруты и эндпоинты
├── models/        # Pydantic модели для валидации данных
├── services/      # Бизнес-логика и взаимодействие с API
├── utils/         # Утилиты (аутентификация, файлы)
├── templates/     # HTML шаблоны для веб-интерфейса
├── static/        # Статические файлы (CSS, JS, изображения)
├── config.py      # Конфигурация приложения
└── main.py        # Точка входа FastAPI приложения
```

## 🛠️ Технологический стек

- **Python 3.11+** - Основной язык программирования
- **FastAPI** - Современный веб-фреймворк для API
- **Pydantic** - Валидация данных и настройки
- **httpx** - Асинхронный HTTP клиент для взаимодействия с Strava API
- **Jinja2** - Шаблонизатор для веб-интерфейса
- **Poetry** - Управление зависимостями
- **Docker** - Контейнеризация приложения
- **Bootstrap 5** - CSS фреймворк для интерфейса

## 📋 Требования

- Python 3.11 или выше
- Poetry (для управления зависимостями)
- Docker и Docker Compose (опционально)
- Аккаунт Strava и зарегистрированное приложение

## 🔧 Установка и настройка

### 1. Клонирование репозитория

```bash
git clone <repository-url>
cd strava-noshoes
```

### 2. Настройка Strava API

1. Перейдите на https://www.strava.com/settings/api
2. Создайте новое приложение
3. Получите `Client ID` и `Client Secret`
4. Для получения токенов доступа используйте OAuth flow или инструменты типа Postman

### 3. Настройка окружения

Скопируйте файл с примером конфигурации:

```bash
cp .env.example .env
```

Отредактируйте `.env` файл, указав ваши данные:

```env
# Strava API Configuration
STRAVA_CLIENT_ID=your_client_id_here
STRAVA_CLIENT_SECRET=your_client_secret_here
STRAVA_ACCESS_TOKEN=your_access_token_here
STRAVA_REFRESH_TOKEN=your_refresh_token_here

# Application Configuration
APP_HOST=0.0.0.0
APP_PORT=8000
APP_DEBUG=true

# File Storage
GPX_STORAGE_PATH=./data/gpx
```

### 4. Установка зависимостей

#### Через Poetry (рекомендуется)

```bash
# Установка Poetry (если не установлен)
curl -sSL https://install.python-poetry.org | python3 -

# Установка зависимостей
poetry install

# Активация виртуального окружения
poetry shell
```

#### Через pip

```bash
# Создание виртуального окружения
python -m venv venv
source venv/bin/activate  # Linux/Mac
# или
venv\Scripts\activate     # Windows

# Установка зависимостей
pip install -r requirements.txt
```

### 5. Запуск приложения

#### Локальный запуск

```bash
# Через Poetry
poetry run python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# Или напрямую
python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# Или через main.py
python app/main.py
```

#### Запуск через Docker

```bash
# Сборка и запуск
docker-compose up --build

# Запуск в фоновом режиме
docker-compose up -d
```

## 🌐 Использование

### Веб-интерфейс

После запуска приложение будет доступно по адресу:
- **Главная страница**: http://localhost:8000
- **API документация**: http://localhost:8000/docs
- **ReDoc документация**: http://localhost:8000/redoc

### API Эндпоинты

#### Основные эндпоинты:

- `GET /api/v1/athlete` - Информация об атлете
- `GET /api/v1/activities` - Список активностей с фильтрацией
- `GET /api/v1/activities/{id}` - Детальная информация об активности
- `GET /api/v1/activities/no-gear` - Активности без экипировки
- `GET /api/v1/activities/running` - Только беговые активности
- `GET /api/v1/gear` - Список экипировки
- `POST /api/v1/activities/{id}/download-gpx` - Загрузка GPX файла
- `GET /api/v1/stats/summary` - Статистика активностей

#### Примеры запросов:

```bash
# Получить информацию об атлете
curl http://localhost:8000/api/v1/athlete

# Получить последние 10 активностей
curl "http://localhost:8000/api/v1/activities?per_page=10"

# Получить только беговые активности
curl "http://localhost:8000/api/v1/activities?activity_type=Run"

# Получить активности без экипировки
curl http://localhost:8000/api/v1/activities/no-gear

# Скачать GPX файл
curl -X POST http://localhost:8000/api/v1/activities/123456/download-gpx
```

### Фильтрация активностей

Поддерживаются следующие параметры фильтрации:

- `before` - Активности до указанной даты
- `after` - Активности после указанной даты
- `page` - Номер страницы (по умолчанию 1)
- `per_page` - Количество на странице (1-200, по умолчанию 30)
- `activity_type` - Тип активности (Run, Ride, Walk и т.д.)
- `has_gear` - Наличие экипировки (true/false)
- `gear_id` - Конкретная экипировка по ID

## 🔒 Аутентификация

Приложение использует OAuth 2.0 для аутентификации со Strava. Токены автоматически обновляются при необходимости.

### Получение токенов доступа

1. Используйте утилиту аутентификации в `app/utils/auth.py`
2. Или получите токены через OAuth flow вручную
3. Или используйте инструменты типа Postman для OAuth

## 📁 Структура данных

### GPX файлы

GPX файлы сохраняются в директории, указанной в `GPX_STORAGE_PATH` (по умолчанию `./data/gpx`).

Имена файлов формируются как: `{activity_id}_{activity_name}.gpx`

### Модели данных

Все модели данных определены в `app/models/strava.py` с использованием Pydantic для валидации.

## 🐳 Docker

### Dockerfile

Приложение упаковано в Docker контейнер на базе Python 3.11-slim с оптимизациями для продакшена.

### Docker Compose

```bash
# Запуск
docker-compose up -d

# Просмотр логов
docker-compose logs -f

# Остановка
docker-compose down

# Пересборка
docker-compose up --build
```

## 🧪 Тестирование

```bash
# Запуск тестов
poetry run pytest

# Запуск с покрытием
poetry run pytest --cov=app

# Запуск линтеров
poetry run black app/
poetry run isort app/
poetry run flake8 app/
poetry run mypy app/
```

## 📊 Мониторинг

### История показателей Garmin

На Garmin-экземпляре приложение собирает 15 групп показателей здоровья:
сон, HRV, Body Battery, стресс, пульс, пульс покоя, шаги, SpO2, дыхание,
суточную сводку, готовность к тренировке, тренировочный статус,
максимальные физиологические показатели, вес/состав тела и гидратацию.
Полные JSON-ответы с временными рядами сохраняются по дням в таблице
`garmin_health` локальной SQLite-базы для будущих дашбордов.

Сбор работает при запуске и каждые 6 часов: обновляет последние 7 дней
и постепенно догружает предыдущие 90 дней. Прогресс и ошибки доступны
в `GET /api/v1/backup/status`, раздел `garmin_health`.
Подробности, настройки глубины истории и обновление контейнера —
в [руководстве по развёртыванию](DOCKER_DEPLOYMENT.md#сбор-показателей-здоровья-garmin).

### Перенос экипировки из Strava

`python -m scripts.migrate_gear` строит отчёт о переносе Gear и привязок
за весь период с первой тренировки Garmin из локальной SQLite-копии Strava;
Strava API и токены
не нужны. Доступные даты архива указаны в отчёте. Добавьте `--apply` для
применения. Однозначные пары
тренировок определяются по времени, спорту, дистанции и длительности;
пробег Garmin пересчитывает по привязкам. Повторный запуск не создаёт
дубли; конфликты пропускаются, пока явно не указан `--replace-existing`.
Команды для Docker и выбор диапазона дат — в
[инструкции](DOCKER_DEPLOYMENT.md#перенос-gear-из-strava-в-garmin).

Приложение включает:

- Health check эндпоинт (`/health`)
- Логирование запросов
- Метрики производительности
- Docker health checks

## 🚀 Развертывание

### Продакшен настройки

1. Установите `APP_DEBUG=false` в `.env`
2. Используйте HTTPS для продакшена
3. Настройте reverse proxy (nginx)
4. Используйте внешнюю базу данных для хранения токенов
5. Настройте мониторинг и логирование

### Переменные окружения для продакшена

```env
APP_DEBUG=false
APP_HOST=0.0.0.0
APP_PORT=8000
DATABASE_URL=postgresql://user:pass@localhost/strava_noshoes
```

## 🤝 Вклад в проект

1. Форкните репозиторий
2. Создайте ветку для новой функции (`git checkout -b feature/amazing-feature`)
3. Зафиксируйте изменения (`git commit -m 'Add amazing feature'`)
4. Отправьте в ветку (`git push origin feature/amazing-feature`)
5. Откройте Pull Request

## 📝 Лицензия

Этот проект распространяется под лицензией MIT. См. файл `LICENSE` для подробностей.

## 🆘 Поддержка

Если у вас возникли проблемы:

1. Проверьте документацию API: http://localhost:8000/docs
2. Убедитесь, что токены Strava действительны
3. Проверьте логи приложения
4. Создайте issue в репозитории

## 📚 Полезные ссылки

- [Strava API Documentation](https://developers.strava.com/docs/reference/)
- [FastAPI Documentation](https://fastapi.tiangolo.com/)
- [Pydantic Documentation](https://pydantic-docs.helpmanual.io/)
- [Poetry Documentation](https://python-poetry.org/docs/)

---

**Strava NoShoes** - ваш надежный спутник для анализа беговых активностей! 🏃‍♂️👟
