FROM python:3.12-slim

# libpq for psycopg, postgresql-client for `psql` inside the container.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       build-essential libpq-dev postgresql-client make \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Source is bind-mounted at runtime (see docker-compose volumes),
# so we don't COPY the app in -- keeps rebuilds fast during dev.
