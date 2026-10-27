# Образ бота. Собирается из корня репозитория, потому что боту нужны webapp/menu.json и webapp/bakeries.js:
#   cd bot && docker compose up -d --build
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY bot/requirements.txt bot/requirements.txt
RUN pip install -r bot/requirements.txt

# Код бота и общие с мини-аппом данные. Токена в образе нет: .env подключается снаружи.
COPY bot/*.py bot/
COPY webapp/menu.json webapp/bakeries.js webapp/

# Не от root. Папки logs и data монтируются с хоста, владелец на хосте должен быть uid 1000.
RUN useradd --uid 1000 --create-home bot \
    && mkdir -p bot/logs bot/data \
    && chown -R bot:bot bot/logs bot/data
USER bot

CMD ["python", "bot/bot.py"]
