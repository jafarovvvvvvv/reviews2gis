# Bedel — панель мониторинга отзывов 2ГИС.
# База с уже установленным Chromium для Playwright (под версию из requirements).
FROM mcr.microsoft.com/playwright/python:v1.60.0-noble

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# В контейнере слушаем все интерфейсы — наружу торчит только Caddy (см. compose)
ENV PANEL_BIND=0.0.0.0 PORT=8765
EXPOSE 8765

CMD ["python", "app.py"]
