FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY local_llm_api ./local_llm_api
RUN pip install .

# 以非 root 身分執行；/app/data 存放對話記憶的 SQLite 檔案。
RUN useradd --system --uid 10001 --home-dir /app app \
    && mkdir -p /app/data \
    && chown app:app /app/data
USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"]

CMD ["python", "-m", "local_llm_api"]
