# The default chronobox image (the machine's system when no other image is given): Debian with
# Python, PostgreSQL, nginx and common tools. Built with `chronobox build --dockerfile`.
FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends \
        postgresql postgresql-client git procps iproute2 curl jq sqlite3 \
        nginx-light less vim-tiny psmisc lsof \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir "psycopg[binary]==3.2.*" "setuptools>=70" wheel "uv==0.8.*" "pytest==8.*"
RUN mkdir -p /run/postgresql && chown postgres:postgres /run/postgresql && chmod 2775 /run/postgresql \
    && chmod 755 /root
