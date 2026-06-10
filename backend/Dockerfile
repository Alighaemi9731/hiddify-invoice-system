FROM python:3.12-slim@sha256:090ba77e2958f6af52a5341f788b50b032dd4ca28377d2893dcf1ecbdfdfe203

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Minimal deps. postgresql-client provides pg_dump/psql for DB backup & restore.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl postgresql-client \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.lock .
RUN pip install --require-hashes -r requirements.lock \
    && pip check

COPY . .

# Runtime data (sqlite fallback, generated invoice PDFs, fixtures).
RUN mkdir -p /app/data /app/invoices

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
