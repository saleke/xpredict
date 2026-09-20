# LISA Production Dockerfile
# Zero external dependencies: pure Python standard library runtime
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/engine \
    LISA_STORAGE=sqlite \
    LISA_DATABASE_URL=/app/data/lisa.db

WORKDIR /app

# Create unprivileged application user
RUN groupadd -g 1001 lisa && \
    useradd -u 1001 -g lisa -s /bin/bash -m lisa

# Copy engine and web static assets
COPY --chown=lisa:lisa engine/ /app/engine/
COPY --chown=lisa:lisa web/ /app/web/

# Create persistent data directory with proper permissions
RUN mkdir -p /app/data && chown -R lisa:lisa /app/data

USER lisa

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health')" || exit 1

ENTRYPOINT ["python3", "-m", "lisa"]
CMD ["start", "--port", "8080"]
