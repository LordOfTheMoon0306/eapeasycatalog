# Smart Catalog

Web-приложение для поиска товаров в четырех источниках: Kaspi Магазин, Wildberries, Ozon и Satu.kz.

## Что реализовано

- Backend на FastAPI
- Frontend на React + Vite
- Поиск по 4 источникам с асинхронным параллельным сбором
- Вкладки по источникам, включая Satu.kz
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

В проекте уже добавлен fallback на browser rendering через `nodriver`. Если прямой `httpx + BeautifulSoup` не срабатывает, адаптер пытается получить HTML через headless Chrome (`--headless=new`).

### Текущие заметки по источникам

- **Kaspi**: поиск может открываться в mobile layout. В этом DOM цены находятся не только в старых `.item-card__prices-price`, но и в `.product-card-info__product-price`, `.product-price__final-price`, `.product-price__final-price-discounted`, `.product-price`. Monthly-payment/рассрочка не должна использоваться как цена товара.
- **Kaspi details**: страница деталей содержит много посторонних чисел, поэтому frontend использует данные кликнутой search-card как источник истины для `title`, `image_url`, `price`, `rating`, `reviews_count`, а detail response нужен в основном для `description`, `characteristics`, `raw_sections`.
- **Wildberries**: если в логах видно `Unsolvable anti-bot challenge detected` или `HTTP 498`, это означает, что WB показал anti-bot/slider challenge вместо HTML с товарами. В этом случае парсеру нечего парсить; нужны рабочие прокси или повтор позже. Captcha bypass в проект не добавлен.
- **Satu.kz**: данные приходят через внешний analyzer endpoint и должны отображаться во вкладке `Satu`.

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
- `backend/app/adapters/` - адаптеры Kaspi/WB/Ozon/Satu и browser fallback через `nodriver`
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
cp .env.example .env
cd ..
python run_backend.py
```

Для browser rendering должен быть установлен Google Chrome. Отдельная установка Playwright/Chromium не требуется для текущего `nodriver` fallback.

Перед запуском добавьте API-ключ в `backend/.env`:

```env
APIFY_API_KEY=apify_api_xxxxxxxxxxxxxxxxxxxxxxxxx
```

Если ключ не указан, приложение продолжит работать, но Ozon/WB будут получать детали только через fallback-парсинг.

В Windows для локального запуска backend можно использовать:

```bash
python run_backend.py
```

`run_backend.py` запускает Uvicorn с `reload=False`, поэтому после изменений backend-кода нужно вручную остановить и заново запустить backend.

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
- `GET /api/product-details?source=kaspi|wildberries|ozon|satu&product_url=...`
- `POST /api/proxies/file` (multipart txt)
- `POST /api/proxies/text`
- `POST /api/proxies/toggle`
- `GET /api/proxies/status`
- `GET /api/proxies/errors`
