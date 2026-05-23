"""Phase 1 scraper: zahcomputers.pk sitemap_index -> product pages -> NocoDB.

Flow:
  1. Solve Cloudflare via FlareSolverr (cached on disk; ~hourly TTL).
  2. Load existing Products from NocoDB — gives us "what's new" and "what was
     scraped recently enough to skip".
  3. Walk sitemap_index.xml -> product-sitemap*.xml -> ~9.6k product URLs.
  4. Async-fetch every product page through the cached CF cookies + UA.
     Parse each page to a flat dict (see parse.py). On 403/503 we assume CF
     re-challenged us, re-solve once via FlareSolverr, and retry.
  5. Diff each batch against `existing`; insert new, update changed; append a
     PriceHistory row only when price or stock actually changed. Flushed every
     BATCH_SIZE so an interrupted run keeps everything already written.
  6. Products that vanished from the sitemap entirely are flagged IsActive = false.

Entry point: python -m src.scraper
"""

import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Awaitable, Callable

import httpx
from dotenv import load_dotenv

from .flaresolverr import CloudflareSession, redact_proxy
from .nocodb import NocoDB
from .parse import (
    is_likely_product_url,
    is_product_sitemap,
    parse_product,
    parse_sitemap_urls,
    slug_from_url,
)
from .schema import ensure_schema

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("zah-scraper")


def _parse_proxy_pool(value: str) -> list[str]:
    """Comma- or whitespace-separated; empty/blank entries dropped."""
    if not value:
        return []
    return [u.strip() for u in value.replace("\n", ",").split(",") if u.strip()]


@dataclass
class Config:
    sitemap_url: str = os.getenv("SITEMAP_URL", "https://zahcomputers.pk/sitemap_index.xml")
    site_origin: str = os.getenv("SITE_ORIGIN", "https://zahcomputers.pk/")
    flaresolverr_url: str = os.getenv("FLARESOLVERR_URL", "")
    cf_session_ttl_seconds: int = int(os.getenv("CF_SESSION_TTL_SECONDS", "3600"))
    # Comma-separated pool. Today's proxy = pool[date.today().toordinal() % len(pool)].
    # On startup-solve failure we walk forward. Empty pool = fetch directly from this host.
    outbound_proxy_urls: list[str] = field(
        default_factory=lambda: _parse_proxy_pool(os.getenv("OUTBOUND_PROXY_URLS", ""))
    )
    concurrency: int = int(os.getenv("CONCURRENCY", "2"))
    request_delay_ms: int = int(os.getenv("REQUEST_DELAY_MS", "1500"))
    timeout: float = float(os.getenv("REQUEST_TIMEOUT", "30"))
    base_id: str = os.getenv("NOCODB_BASE_ID", "")
    limit: int = int(os.getenv("SCRAPE_LIMIT", "0"))  # 0 = scrape everything
    rescrape_after_hours: int = int(os.getenv("RESCRAPE_AFTER_HOURS", "12"))
    force: bool = os.getenv("FORCE_RESCRAPE", "false").lower() == "true"


class Throttle:
    """Adaptive rate limiter shared by all fetch workers.

    Every worker calls `await wait()` before each request. A 429 pauses *all*
    workers (honoring `Retry-After`) and permanently raises the base delay, so
    the scraper self-tunes below the limit instead of retry-spamming into it.
    """

    def __init__(self, base_delay_ms: int, max_delay_s: float = 8.0):
        self._delay = base_delay_ms / 1000
        self._max_delay = max_delay_s
        self._pause_until = 0.0

    async def wait(self) -> None:
        now = time.monotonic()
        if self._pause_until > now:
            await asyncio.sleep(self._pause_until - now)
        await asyncio.sleep(self._delay)

    def back_off(self, pause_s: float, *, slow_down: bool) -> None:
        self._pause_until = max(self._pause_until, time.monotonic() + pause_s)
        if slow_down:
            self._delay = min(self._delay * 1.5, self._max_delay)
            log.warning("rate limited — pausing %.0fs, base delay now %.1fs", pause_s, self._delay)

    @property
    def delay(self) -> float:
        return self._delay


class CfRefresher:
    """Cloudflare cookie refresher with single-flight semantics.

    When a worker hits 403/503, several other in-flight workers usually hit it
    too. The lock means only ONE re-solve runs; the others see the refresh just
    happened (within `min_refresh_gap_s`) and skip — their next request picks
    up the new cookies automatically because httpx.AsyncClient.cookies is
    shared mutable state.
    """

    def __init__(self, session: CloudflareSession, client: httpx.AsyncClient, site_origin: str):
        self._session = session
        self._client = client
        self._site_origin = site_origin
        self._lock = asyncio.Lock()
        self._last_refreshed = 0.0

    async def refresh_if_stale(self, min_refresh_gap_s: float = 10.0) -> None:
        async with self._lock:
            if time.monotonic() - self._last_refreshed < min_refresh_gap_s:
                return
            await asyncio.to_thread(self._session.invalidate)
            cookies, ua = await asyncio.to_thread(self._session.get, self._site_origin, True)
            self._client.cookies.clear()
            for k, v in cookies.items():
                self._client.cookies.set(k, v, domain="zahcomputers.pk")
            self._client.headers["user-agent"] = ua
            self._last_refreshed = time.monotonic()
            log.info("refreshed Cloudflare session (%d cookies)", len(cookies))


def _retry_after(resp: httpx.Response, default: float) -> float:
    try:
        return max(float(resp.headers.get("retry-after", "")), default)
    except ValueError:
        return default


def _looks_like_cf_challenge(text: str) -> bool:
    """A 403/503 may also be a real upstream error; the CF challenge body has
    distinctive markers. Used to decide whether to bother re-solving."""
    head = text[:2000].lower()
    return "just a moment" in head or "challenge-platform" in head or "cf-mitigated" in head


async def fetch(
    client: httpx.AsyncClient,
    url: str,
    throttle: Throttle,
    refresher: CfRefresher | None = None,
    max_attempts: int = 4,
) -> str:
    """Fetch a URL through the shared throttle. Raises on persistent failure."""
    last_error: Exception = RuntimeError(f"no attempt made for {url}")
    for attempt in range(max_attempts):
        await throttle.wait()
        try:
            r = await client.get(url, follow_redirects=True)
        except httpx.TransportError as e:
            last_error = e
            await asyncio.sleep(2**attempt)
            continue

        if r.status_code in (403, 503) and refresher is not None and _looks_like_cf_challenge(r.text):
            log.warning("CF challenge on %s -> re-solving (attempt %d)", url, attempt + 1)
            await refresher.refresh_if_stale()
            last_error = httpx.HTTPStatusError(
                f"cloudflare challenge {r.status_code}", request=r.request, response=r,
            )
            continue
        if r.status_code == 429:
            throttle.back_off(_retry_after(r, 60), slow_down=True)
            last_error = httpx.HTTPStatusError("429 Too Many Requests", request=r.request, response=r)
            continue
        if r.status_code >= 500:
            throttle.back_off(5, slow_down=False)
            last_error = httpx.HTTPStatusError(f"server error {r.status_code}", request=r.request, response=r)
            continue
        if r.status_code >= 400:
            # Non-CF 4xx: deleted product / blocked URL — fail fast, no retry.
            raise httpx.HTTPStatusError(f"{r.status_code}", request=r.request, response=r)
        return r.text
    raise last_error


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_dt(value) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _norm_price(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def _product_fields(p: dict, now: str, *, is_new: bool) -> dict:
    """Build the NocoDB column dict for the Products table. Field names must
    match schema.PRODUCTS_COLUMNS exactly — NocoDB silently drops unknown keys."""
    fields = {
        "Slug": p["slug"],
        "WpPostId": p["wp_post_id"],
        "URL": p["url"],
        "Title": p["title"],
        "Brand": p["brand"],
        "Category": p["category"],
        "CategoryPath": p["category_path"],
        "SKU": p["sku"],
        "CurrentPrice": p["price"],
        "Currency": p["currency"],
        "InStock": bool(p["in_stock"]),
        "Availability": p["availability"],
        "ShortDescriptionText": p["short_description_text"],
        "ShortDescriptionHtml": p["short_description_html"],
        "DescriptionHtml": p["description_html"],
        "Attributes": json.dumps(p["attributes"] or {}, ensure_ascii=False),
        "Images": json.dumps(p["images"] or []),
        "LastSeen": now,
        "IsActive": True,
    }
    if is_new:
        fields["FirstSeen"] = now
    return fields


def _price_row(p: dict, now: str) -> dict:
    return {
        "Slug": p["slug"],
        "ScrapedAt": now,
        "Price": p["price"],
        "Currency": p["currency"],
        "InStock": bool(p["in_stock"]),
        "Availability": p["availability"],
    }


BATCH_SIZE = 50


async def _walk_sitemaps(cfg: Config, client: httpx.AsyncClient, throttle: Throttle, refresher: CfRefresher) -> list[str]:
    """Two-stage sitemap walk: sitemap_index.xml -> product-sitemap*.xml ->
    ~9.6k product URLs. Other sub-sitemaps (posts, pages, taxonomies) are
    filtered out."""
    log.info("fetching sitemap_index %s", cfg.sitemap_url)
    index_xml = await fetch(client, cfg.sitemap_url, throttle, refresher=refresher)
    sub_sitemaps = [u for u in parse_sitemap_urls(index_xml) if is_product_sitemap(u)]
    log.info("%d product sub-sitemap(s) found", len(sub_sitemaps))

    all_urls: list[str] = []
    for sub in sub_sitemaps:
        xml = await fetch(client, sub, throttle, refresher=refresher)
        urls = [u for u in parse_sitemap_urls(xml) if is_likely_product_url(u)]
        all_urls.extend(urls)
        log.info("  %s: %d product URLs", sub.rsplit("/", 1)[-1], len(urls))
    log.info("%d total candidate product URLs", len(all_urls))
    return all_urls


async def _prime_cf_session(cfg: Config) -> tuple[CloudflareSession, str]:
    """Pick today's proxy from the pool and prime a CloudflareSession.

    Rotation: `pool[date.today().toordinal() % len(pool)]`. If that proxy fails
    to solve the challenge, walk forward through the list until one succeeds.
    Returns (session, active_proxy_url); active_proxy_url is "" when the pool
    is empty (direct mode from this host).
    """
    pool = cfg.outbound_proxy_urls
    if not pool:
        log.info("no outbound proxy configured — fetching from this host's IP")
        session = CloudflareSession(
            flaresolverr_url=cfg.flaresolverr_url,
            session_ttl_seconds=cfg.cf_session_ttl_seconds,
        )
        await asyncio.to_thread(session.get, cfg.site_origin)
        return session, ""

    start_idx = date.today().toordinal() % len(pool)
    log.info("proxy pool: %d entries; starting at index %d (daily rotation)", len(pool), start_idx)

    last_error: Exception | None = None
    for offset in range(len(pool)):
        proxy = pool[(start_idx + offset) % len(pool)]
        log.info("trying proxy %s", redact_proxy(proxy))
        session = CloudflareSession(
            flaresolverr_url=cfg.flaresolverr_url,
            proxy_url=proxy,
            session_ttl_seconds=cfg.cf_session_ttl_seconds,
        )
        try:
            await asyncio.to_thread(session.get, cfg.site_origin)
        except Exception as e:
            log.warning("proxy %s failed to solve: %s", redact_proxy(proxy), e)
            last_error = e
            continue
        log.info("active proxy: %s", redact_proxy(proxy))
        return session, proxy

    raise RuntimeError(f"all {len(pool)} proxies failed to solve Cloudflare; last error: {last_error}")


async def _scrape_all(
    cfg: Config,
    should_scrape: Callable[[str], bool],
    on_batch: Callable[[list[dict]], Awaitable[None]],
) -> tuple[int, int, set[str]]:
    """Scrape product pages, handing parsed products to `on_batch` in batches
    of BATCH_SIZE so they are persisted as the scrape progresses.

    Returns (failed_count, scraped_url_count, every_slug_in_sitemap)."""
    if not cfg.flaresolverr_url:
        raise RuntimeError("FLARESOLVERR_URL must be set — zahcomputers is behind Cloudflare")

    session, active_proxy = await _prime_cf_session(cfg)
    cookies, user_agent = session.get(cfg.site_origin)   # already warm in cache from _prime

    headers = {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    limits = httpx.Limits(max_connections=cfg.concurrency, max_keepalive_connections=cfg.concurrency)
    throttle = Throttle(cfg.request_delay_ms)
    failed = 0

    async with httpx.AsyncClient(
        headers=headers,
        cookies=cookies,
        timeout=cfg.timeout,
        limits=limits,
        http2=True,
        proxy=active_proxy or None,
    ) as client:
        refresher = CfRefresher(session, client, cfg.site_origin)

        candidate_urls = await _walk_sitemaps(cfg, client, throttle, refresher)
        sitemap_slugs = {slug_from_url(u) for u in candidate_urls}

        urls = candidate_urls
        if cfg.limit and cfg.limit < len(urls):
            stride = max(1, len(urls) // cfg.limit)
            urls = urls[::stride][: cfg.limit]
            log.info("SCRAPE_LIMIT active -> sampled %d URLs", len(urls))

        before = len(urls)
        urls = [u for u in urls if should_scrape(slug_from_url(u))]
        if len(urls) < before:
            log.info(
                "skipping %d product(s) scraped within the last %dh",
                before - len(urls), cfg.rescrape_after_hours,
            )
        if not urls:
            log.info("nothing to scrape — every candidate is still fresh")
            return failed, 0, sitemap_slugs

        sem = asyncio.Semaphore(cfg.concurrency)

        async def worker(url: str):
            async with sem:
                try:
                    html = await fetch(client, url, throttle, refresher=refresher)
                    return parse_product(html, url)
                except Exception as e:
                    log.warning("failed %s: %s", url, e)
                    return e

        tasks = [asyncio.create_task(worker(u)) for u in urls]
        batch: list[dict] = []
        done = 0
        for fut in asyncio.as_completed(tasks):
            result = await fut
            done += 1
            if isinstance(result, Exception):
                failed += 1
            elif result is not None:
                batch.append(result)
                if len(batch) >= BATCH_SIZE:
                    await on_batch(batch)
                    batch = []
            if done % 100 == 0:
                log.info("scraped %d/%d (failed=%d, delay=%.1fs)", done, len(urls), failed, throttle.delay)
        if batch:
            await on_batch(batch)

    return failed, len(urls), sitemap_slugs


def run(cfg: Config) -> int:
    log.info("target NocoDB: base %s @ %s", cfg.base_id, os.getenv("NOCODB_BASE_URL", "?"))

    with NocoDB() as db:
        tables = ensure_schema(db, cfg.base_id)
        log.info("loading existing products from NocoDB")
        existing_records = db.list_all(
            tables["Products"],
            fields=["Id", "Slug", "CurrentPrice", "InStock", "LastSeen"],
        )
    existing = {r["Slug"]: r for r in existing_records if r.get("Slug")}
    log.info("%d products already in NocoDB", len(existing))

    cutoff = datetime.now(timezone.utc) - timedelta(hours=cfg.rescrape_after_hours)

    def should_scrape(slug: str) -> bool:
        if cfg.force:
            return True
        rec = existing.get(slug)
        if rec is None:
            return True
        last_seen = _parse_dt(rec.get("LastSeen"))
        return last_seen is None or last_seen < cutoff

    stats = {"new": 0, "updated": 0, "price_rows": 0}

    def write_batch(products_batch: list[dict]) -> None:
        now = _now_iso()
        new_rows: list[dict] = []
        upd_rows: list[dict] = []
        price_rows: list[dict] = []
        for p in products_batch:
            rec = existing.get(p["slug"])
            if rec:
                row = _product_fields(p, now, is_new=False)
                row["Id"] = rec["Id"]
                upd_rows.append(row)
                price_changed = _norm_price(rec.get("CurrentPrice")) != _norm_price(p["price"])
                stock_changed = bool(rec.get("InStock")) != bool(p["in_stock"])
                if price_changed or stock_changed:
                    price_rows.append(_price_row(p, now))
            else:
                new_rows.append(_product_fields(p, now, is_new=True))
                price_rows.append(_price_row(p, now))   # first observation
        with NocoDB() as db:
            if new_rows:
                db.bulk_create(tables["Products"], new_rows)
            if upd_rows:
                db.bulk_update(tables["Products"], upd_rows)
            if price_rows:
                db.bulk_create(tables["PriceHistory"], price_rows)
        stats["new"] += len(new_rows)
        stats["updated"] += len(upd_rows)
        stats["price_rows"] += len(price_rows)
        log.info(
            "flushed %d products -> NocoDB (totals: new=%d updated=%d price_rows=%d)",
            len(products_batch), stats["new"], stats["updated"], stats["price_rows"],
        )

    async def on_batch(products_batch: list[dict]) -> None:
        await asyncio.to_thread(write_batch, products_batch)

    failed, scraped, sitemap_slugs = asyncio.run(_scrape_all(cfg, should_scrape, on_batch))

    deactivations = [
        {"Id": rec["Id"], "IsActive": False}
        for slug, rec in existing.items()
        if slug not in sitemap_slugs and rec.get("IsActive")
    ]
    if deactivations:
        with NocoDB() as db:
            db.bulk_update(tables["Products"], deactivations)

    log.info(
        "done: new=%d updated=%d price_rows=%d deactivated=%d (failed=%d/%d scraped)",
        stats["new"], stats["updated"], stats["price_rows"],
        len(deactivations), failed, scraped,
    )
    return 1 if scraped and failed > scraped * 0.2 else 0


def main() -> None:
    cfg = Config()
    if not cfg.base_id:
        log.error("NOCODB_BASE_ID must be set")
        sys.exit(2)
    if not cfg.flaresolverr_url:
        log.error("FLARESOLVERR_URL must be set — zahcomputers is behind Cloudflare")
        sys.exit(2)
    sys.exit(run(cfg))


if __name__ == "__main__":
    main()
