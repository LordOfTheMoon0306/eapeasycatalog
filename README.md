# Smart Catalog

Web-приложение для поиска товаров в трех источниках: Kaspi Магазин, Wildberries и Ozon.

## Что реализовано

- Backend на FastAPI
- Frontend на React + Vite
- Поиск по 3 источникам с асинхронным параллельным сбором
- Вкладки по источникам (по умолчанию Kaspi)
- До 10 товаров на вкладку
- Карточки товара: название, фото, цена, кнопка "Открыть"
- Внутренняя страница деталей товара
- Панель прокси:
  - загрузка через txt
  - загрузка через textarea
  - включение/выключение прокси
  - ротация прокси
  - fallback на следующий прокси
  - retry + timeout
  - лог ошибок прокси
- CORS, обработка ошибок, базовые anti-bot headers

## Ограничения парсинга маркетплейсов

Kaspi, Wildberries и Ozon активно используют динамическую отрисовку и антибот-защиту. Это означает:

- селекторы могут часто меняться;
- часть данных может быть недоступна без JS/браузерного рендера;
- возможны 403/429 даже с прокси.

В проекте уже добавлен fallback на Playwright. Если прямой `httpx + BeautifulSoup` не срабатывает, адаптер пытается получить HTML через headless browser.

### Новые настройки устойчивости и rollout

- `DEVICE_PROFILE_DEFAULT` - профиль по умолчанию для источников (`desktop` или `mobile`).
- `KASPI_DEVICE_PROFILE` - отдельный профиль для Kaspi (в первой итерации можно включать `mobile`, не затрагивая WB/Ozon).
- `OZON_DEVICE_PROFILE` - профиль для Ozon (`desktop` или `mobile`).
- `WILDBERRIES_DEVICE_PROFILE` - профиль для Wildberries (`desktop` или `mobile`).
- `SOURCE_CONCURRENCY_LIMIT` - ограничение параллельных запросов на один источник.
- `SOURCE_MIN_INTERVAL_SECONDS` - минимальный интервал между запросами к одному источнику.
- `ENABLE_BLOCK_TELEMETRY` - включение структурированных событий блокировок в логах.
- `APIFY_API_KEY` - ваш личный ключ Apify для обогащения карточек Ozon/WB.
- `APIFY_TOKEN` - альтернативное имя переменной (если уже используете такое в окружении).
- `APIFY_OZON_ACTOR_ID` - actor для Ozon (по умолчанию `zen-studio/ozon-scraper-pro`).
- `APIFY_WILDBERRIES_ACTOR_ID` - actor для WB (по умолчанию `akoinc/wb-card-parser`).

## Структура

- `backend/app/main.py` - FastAPI приложение
- `backend/app/api/routes.py` - API маршруты
- `backend/app/core/proxy_manager.py` - ротация/статусы/ошибки прокси
- `backend/app/core/http_client.py` - httpx клиент с retry/fallback
- `backend/app/adapters/` - адаптеры Kaspi/WB/Ozon
- `frontend/src/` - React UI

## Запуск

### Через Docker Compose (рекомендуется)

```bash
cp .env.example backend/.env
# при необходимости добавьте APIFY_API_KEY и другие переменные в backend/.env
docker compose up -d --build
```

- Frontend: http://localhost:5173 (nginx раздаёт production build и проксирует `/api` на backend).
- Backend API: http://localhost:8000 (например, `curl http://localhost:8000/api/health`).

Остановить: `docker compose down`.

### Вручную

### 1. Backend

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -r ../requirements.txt
playwright install chromium
cp .env.example .env
uvicorn app.main:app --reload --port 8000
```

Перед запуском добавьте API-ключ в `backend/.env`:

```env
APIFY_API_KEY=apify_api_xxxxxxxxxxxxxxxxxxxxxxxxx
```

Если ключ не указан, приложение продолжит работать, но Ozon/WB будут получать детали только через fallback-парсинг.

### 2. Frontend

```bash
cd frontend
npm install
npm run dev
```

Frontend поднимется на `http://localhost:5173`, backend на `http://localhost:8000`.

## API

- `GET /api/health`
- `GET /api/search?query=...`
- `GET /api/product-details?source=kaspi|wildberries|ozon&product_url=...`
- `POST /api/proxies/file` (multipart txt)
- `POST /api/proxies/text`
- `POST /api/proxies/toggle`
- `GET /api/proxies/status`
- `GET /api/proxies/errors`

