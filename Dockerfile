# ZAH data scraper — light (no browser; Cloudflare bypass is via the external
# FlareSolverr container). Writes scraped products to Postgres via PgDB.
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app

WORKDIR /app
COPY requirements.txt ./
RUN pip install -r requirements.txt
COPY . .

# Default = one scrape pass and exit. docker-compose wraps this in a nightly loop.
CMD ["python", "-m", "src.scraper"]
