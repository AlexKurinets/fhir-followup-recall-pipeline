FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY recall recall
COPY gateway gateway
COPY seed seed
RUN pip install ".[llm]"
COPY config config

# The key and export directories are created here so the named volumes mounted over them
# inherit the non-root owner on first use.
RUN useradd --create-home --uid 10001 app \
    && mkdir -p /keys /data/exports \
    && chown app:app /keys /data/exports
USER app

EXPOSE 8000
