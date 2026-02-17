# Docker Deployment Guide / Руководство по развертыванию через Docker

## 🐳 Развертывание на удаленном сервере

### Быстрый старт

Проект уже включает готовые `Dockerfile` и `docker-compose.yml` для развертывания.

### Шаг 1: Подготовка сервера

**Требования:**
- Linux сервер (Ubuntu 20.04+ / Debian 11+ / CentOS 8+)
- Docker 20.10+
- Docker Compose 2.0+
- Открытые порты: 80 (HTTP), 443 (HTTPS, опционально), 8000 (для разработки)

**Установка Docker:**

```bash
# Ubuntu/Debian
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh
sudo usermod -aG docker $USER

# Установка Docker Compose
sudo apt-get update
sudo apt-get install docker-compose-plugin
```

### Шаг 2: Загрузка проекта на сервер

**Вариант 1: Через Git**

```bash
# Клонируйте репозиторий
git clone <your-repository-url>
cd Strava_NoShoes
```

**Вариант 2: Через SCP/SFTP**

```bash
# С локального компьютера
scp -r Strava_NoShoes user@your-server:/home/user/
```

### Шаг 3: Настройка переменных окружения

Создайте файл `.env` на сервере:

```bash
cd Strava_NoShoes
cp .env.example .env
nano .env  # или vim .env
```

**Обязательные переменные:**

```env
# Strava API
STRAVA_CLIENT_ID=your_client_id
STRAVA_CLIENT_SECRET=your_client_secret
STRAVA_REFRESH_TOKEN=your_refresh_token

# Telegram Bot
BOT_API_TOKEN=your_telegram_bot_token

# Application
APP_HOST=0.0.0.0
APP_PORT=8000
APP_DEBUG=false

# File Storage
GPX_STORAGE_PATH=/app/data/gpx

# GPX Cleanup
GPX_CLEANUP_ENABLED=true
GPX_CLEANUP_SCHEDULE_HOUR=3
GPX_CLEANUP_SCHEDULE_MINUTE=0
```

### Шаг 4: Запуск через Docker Compose

```bash
# Сборка и запуск контейнеров
docker-compose up -d --build

# Проверка логов
docker-compose logs -f

# Проверка статуса
docker-compose ps
```

### Шаг 5: Проверка работоспособности

```bash
# Проверка health check
curl http://localhost:8000/health

# Проверка главной страницы
curl http://localhost:8000
```

Приложение доступно по адресу: `http://your-server-ip:8000`

---

## 🌐 Настройка Nginx (продакшн)

Для продакшн-развертывания рекомендуется использовать Nginx как reverse proxy.

### Создание конфигурации Nginx

Создайте файл `nginx.conf` в корне проекта:

```nginx
events {
    worker_connections 1024;
}

http {
    upstream strava_app {
        server strava-noshoes:8000;
    }

    server {
        listen 80;
        server_name your-domain.com;  # Замените на ваш домен

        client_max_body_size 50M;

        location / {
            proxy_pass http://strava_app;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
            
            # WebSocket support (если нужно)
            proxy_http_version 1.1;
            proxy_set_header Upgrade $http_upgrade;
            proxy_set_header Connection "upgrade";
        }

        # Статические файлы
        location /static {
            proxy_pass http://strava_app/static;
            proxy_cache_valid 200 1d;
            expires 1d;
            add_header Cache-Control "public, immutable";
        }
    }
}
```

### Обновление docker-compose.yml

Раскомментируйте секцию nginx в `docker-compose.yml`:

```yaml
services:
  strava-noshoes:
    # ... существующая конфигурация ...
    ports:
      - "8000:8000"  # Можно убрать для большей безопасности

  nginx:
    image: nginx:alpine
    container_name: strava-noshoes-nginx
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./nginx.conf:/etc/nginx/nginx.conf:ro
      - ./ssl:/etc/nginx/ssl:ro  # Для HTTPS
    depends_on:
      - strava-noshoes
    restart: unless-stopped
```

### Перезапуск с Nginx

```bash
docker-compose down
docker-compose up -d --build
```

---

## 🔒 Настройка HTTPS (Let's Encrypt)

### Установка Certbot

```bash
sudo apt-get update
sudo apt-get install certbot
```

### Получение SSL сертификата

```bash
# Остановите Nginx временно
docker-compose stop nginx

# Получите сертификат
sudo certbot certonly --standalone -d your-domain.com

# Скопируйте сертификаты
sudo mkdir -p ./ssl
sudo cp /etc/letsencrypt/live/your-domain.com/fullchain.pem ./ssl/
sudo cp /etc/letsencrypt/live/your-domain.com/privkey.pem ./ssl/
sudo chmod -R 755 ./ssl
```

### Обновление nginx.conf для HTTPS

```nginx
http {
    # ... upstream ...

    # Редирект HTTP -> HTTPS
    server {
        listen 80;
        server_name your-domain.com;
        return 301 https://$server_name$request_uri;
    }

    # HTTPS сервер
    server {
        listen 443 ssl http2;
        server_name your-domain.com;

        ssl_certificate /etc/nginx/ssl/fullchain.pem;
        ssl_certificate_key /etc/nginx/ssl/privkey.pem;

        ssl_protocols TLSv1.2 TLSv1.3;
        ssl_ciphers HIGH:!aNULL:!MD5;
        ssl_prefer_server_ciphers on;

        client_max_body_size 50M;

        location / {
            proxy_pass http://strava_app;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
        }
    }
}
```

---

## 📊 Управление контейнерами

### Основные команды

```bash
# Запуск
docker-compose up -d

# Остановка
docker-compose down

# Перезапуск
docker-compose restart

# Просмотр логов
docker-compose logs -f strava-noshoes

# Просмотр логов только за последний час
docker-compose logs --since 1h strava-noshoes

# Вход в контейнер
docker-compose exec strava-noshoes bash

# Обновление образа
docker-compose pull
docker-compose up -d --build

# Очистка старых образов
docker system prune -a
```

### Мониторинг

```bash
# Использование ресурсов
docker stats

# Проверка health check
docker inspect strava-noshoes-app | grep -A 10 Health
```

---

## 🔄 Автоматическое обновление

### Создание скрипта обновления

Создайте файл `update.sh`:

```bash
#!/bin/bash

echo "🔄 Updating Strava NoShoes..."

# Переход в директорию проекта
cd /home/user/Strava_NoShoes

# Получение обновлений из Git (если используется)
git pull origin main

# Пересборка и перезапуск контейнеров
docker-compose down
docker-compose up -d --build

# Очистка старых образов
docker image prune -f

echo "✅ Update completed!"
```

Сделайте скрипт исполняемым:

```bash
chmod +x update.sh
```

---

## 🗄️ Резервное копирование

### Бэкап данных

```bash
#!/bin/bash
# backup.sh

BACKUP_DIR="/home/user/backups"
DATE=$(date +%Y%m%d_%H%M%S)

# Создание директории для бэкапов
mkdir -p $BACKUP_DIR

# Бэкап данных (GPX файлы и состояние бота)
tar -czf $BACKUP_DIR/strava_data_$DATE.tar.gz ./data

# Бэкап конфигурации
cp .env $BACKUP_DIR/.env_$DATE

echo "✅ Backup created: $BACKUP_DIR/strava_data_$DATE.tar.gz"

# Удаление старых бэкапов (старше 30 дней)
find $BACKUP_DIR -type f -mtime +30 -delete
```

### Автоматический бэкап через cron

```bash
# Редактирование crontab
crontab -e

# Добавление задачи (каждый день в 2:00)
0 2 * * * /home/user/Strava_NoShoes/backup.sh
```

---

## 🛠️ Troubleshooting

### Проблема: Контейнер не запускается

```bash
# Проверить логи
docker-compose logs strava-noshoes

# Проверить конфигурацию
docker-compose config

# Пересобрать образ
docker-compose build --no-cache
```

### Проблема: Порт занят

```bash
# Найти процесс на порту 8000
sudo lsof -i :8000

# Или изменить порт в docker-compose.yml
ports:
  - "8001:8000"  # Вместо 8000:8000
```

### Проблема: Нет доступа к файлам

```bash
# Проверить права доступа
sudo chown -R 1000:1000 ./data
```

### Проблема: Telegram бот не работает

```bash
# Проверить логи бота
docker-compose logs -f strava-noshoes | grep -i telegram

# Проверить токен в .env
cat .env | grep BOT_API_TOKEN
```

---

## 📈 Масштабирование

### Запуск нескольких инстансов

```yaml
services:
  strava-noshoes:
    # ... конфигурация ...
    deploy:
      replicas: 3  # Количество инстансов
```

### Использование Docker Swarm

```bash
# Инициализация Swarm
docker swarm init

# Деплой стека
docker stack deploy -c docker-compose.yml strava
```

---

## ✅ Итоговый чеклист для развертывания

- [ ] Установлен Docker и Docker Compose
- [ ] Проект скопирован на сервер
- [ ] Настроен файл `.env` со всеми переменными
- [ ] Запущены контейнеры через `docker-compose up -d`
- [ ] Проверен health check: `curl http://localhost:8000/health`
- [ ] Настроен Nginx (опционально)
- [ ] Получен SSL сертификат (опционально)
- [ ] Настроен автоматический бэкап
- [ ] Настроен брандмауэр (UFW/iptables)
- [ ] Приложение доступно по домену/IP

---

## 🔗 Полезные ссылки

- [Docker Documentation](https://docs.docker.com/)
- [Docker Compose Documentation](https://docs.docker.com/compose/)
- [Nginx Documentation](https://nginx.org/en/docs/)
- [Let's Encrypt](https://letsencrypt.org/)

---

## 📞 Поддержка

Если возникли вопросы при развертывании, проверьте:
1. Логи контейнера: `docker-compose logs -f`
2. Health check: `curl http://localhost:8000/health`
3. Переменные окружения в `.env`
4. Права доступа к директории `data/`
