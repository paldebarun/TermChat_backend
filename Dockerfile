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
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*
# libgl1/libglib2.0-0: OpenCV, pulled in by Docling's OCR engine (RapidOCR).

COPY requirements.txt requirements-hermes.txt requirements-parser.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Hermes gets its own venv so its exact dependency pins can't conflict with
# the app's (see requirements-hermes.txt).
RUN python -m venv /opt/hermes-venv \
    && /opt/hermes-venv/bin/pip install --no-cache-dir -r requirements-hermes.txt

# Docling + Whisper venv, same reason (see requirements-parser.txt). CPU-only
# torch wheels: the default Linux ones bundle several GB of CUDA libraries.
RUN python -m venv /opt/parser-venv \
    && /opt/parser-venv/bin/pip install --no-cache-dir \
        --extra-index-url https://download.pytorch.org/whl/cpu \
        -r requirements-parser.txt

# Download every parser model at build time (Docling layout/TableFormer/OCR,
# Whisper) so parsing never needs outbound network at runtime. Only the
# dependency-free worker is copied first, so app code changes don't redo this.
ARG ASSISTANT_WHISPER_MODEL=base
ENV HF_HOME=/opt/models
COPY app/__init__.py ./app/__init__.py
COPY app/assistant/__init__.py app/assistant/parse_worker.py ./app/assistant/
RUN /opt/parser-venv/bin/python -m app.assistant.parse_worker --mode warmup \
        --whisper-model "${ASSISTANT_WHISPER_MODEL}" \
    && chmod -R a+rX /opt/models
ENV HF_HUB_OFFLINE=1

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
