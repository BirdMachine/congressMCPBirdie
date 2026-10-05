FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md requirements.txt ./
COPY congress_api ./congress_api
COPY container-entrypoint.sh ./container-entrypoint.sh
RUN pip install --no-cache-dir -r requirements.txt && pip install --no-cache-dir --no-deps . \
    && useradd --create-home --uid 10001 congress
ENV ENV=production LOG_LEVEL=WARNING PYTHONUNBUFFERED=1 HOME=/home/congress
EXPOSE 8000
ENV BIRDIE_DIGEST_STATE=/var/data/birdie/digest.sqlite3
ENTRYPOINT ["/app/container-entrypoint.sh"]
