FROM python:3.12-slim

WORKDIR /code

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# libpq-dev/build-essential are needed to build asyncpg/cryptography wheels
# on some platforms; harmless to keep even when wheels are prebuilt.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements-hermes.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Hermes gets its own venv so its exact dependency pins can't conflict with
# the app's (see requirements-hermes.txt).
RUN python -m venv /opt/hermes-venv \
    && /opt/hermes-venv/bin/pip install --no-cache-dir -r requirements-hermes.txt

COPY app ./app

RUN useradd --create-home appuser
USER appuser

# Chroma's default embedding model (ONNX MiniLM) is otherwise downloaded on
# the first search/index call, which blocks that request and needs outbound
# network at runtime. Fetch it at build time into appuser's cache.
RUN python -c "from chromadb.utils.embedding_functions import DefaultEmbeddingFunction as D; D()(['warm'])"

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/ || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
