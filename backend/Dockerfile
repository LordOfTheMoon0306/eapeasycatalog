FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    CHROME_PATH=/usr/bin/chromium

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git \
        ca-certificates \
        chromium \
        xvfb \
        dbus-x11 \
        fonts-liberation \
        libnss3 \
        libatk-bridge2.0-0 \
        libatk1.0-0 \
        libcups2 \
        libdrm2 \
        libxkbcommon0 \
        libxcomposite1 \
        libxdamage1 \
        libxrandr2 \
        libxfixes3 \
        libgbm1 \
        libasound2 \
        libpangocairo-1.0-0 \
        libpango-1.0-0 \
        libcairo2 \
        libexpat1 \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/chromium /usr/bin/google-chrome \
    && ln -sf /usr/bin/chromium /usr/bin/chromium-browser

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY backend/app /app/app

EXPOSE 8000

# nodriver запускает Chromium с headless=False (см. backend/app/adapters/fallback_nodriver.py),
# поэтому оборачиваем uvicorn в xvfb-run, чтобы был виртуальный X-дисплей.
CMD ["xvfb-run", "-a", "--server-args=-screen 0 1440x2200x24", \
     "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
