FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    libpango-1.0-0 \
    libpangoft2-1.0-0 \
    libharfbuzz0b \
    libcairo2 \
    libffi-dev \
    shared-mime-info \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install --no-cache-dir uv

COPY pyproject.toml uv.lock ./

RUN uv sync --frozen --no-dev --no-install-project

COPY src/ ./src/
COPY data/ ./data/
COPY web/ ./web/
COPY ui/ ./ui/

RUN uv run python data/seed_olist.py

ENV PORT=10000
EXPOSE 10000

CMD ["sh", "-c", "uv run uvicorn src.api.server:app --host 0.0.0.0 --port ${PORT:-10000}"]
