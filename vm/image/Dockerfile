# Root filesystem for chronobox Firecracker VMs (built on the host, used offline).
FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends \
        postgresql postgresql-client git procps iproute2 curl jq sqlite3 \
        nginx-light less vim-tiny psmisc lsof \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir "psycopg[binary]==3.2.*" "setuptools>=70" wheel "uv==0.8.*" "pytest==8.*"
RUN mkdir -p /newroot /data /opt/chronobox && chmod 755 /root
COPY guest_agent.py /opt/chronobox/guest_agent.py
COPY init.sh /sbin/chronobox-init
RUN chmod +x /sbin/chronobox-init
