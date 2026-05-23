# Zahcomputers.pk price scraper

Scrapes product pricing from [zahcomputers.pk](https://zahcomputers.pk) on a
schedule and stores it in a **self-hosted NocoDB**.

This repo is a sibling of `galaxy-scrapper/` — same skeleton, different target
site, separate NocoDB base.

## How it works

1. **Cloudflare bypass.** zahcomputers.pk sits behind a Cloudflare JS
   challenge. `src/flaresolverr.py` makes a one-time call to a FlareSolverr
   instance (Dockerised undetected-chromedriver) which solves the challenge and
   returns the resulting `cf_clearance` cookie + matching user-agent. Those are
   cached on disk (`.cf-cookies.json`) and replayed on every subsequent fetch
   for ~1 hour, after which the scraper re-solves.
2. **Sitemap walk.** `src/scraper.py` fetches `sitemap_index.xml`, follows the
   ~10 `product-sitemap*.xml` sub-sitemaps, and collects ~9,600 product URLs.
3. **Product extraction.** It fetches each product page through the cookied
   httpx client. Every page emits `schema.org` Product JSON-LD (title, sku,
   price nested under `offers[0].priceSpecification[0]`, availability, the
   featured image) plus a `#tab-description` panel containing the spec table —
   `src/parse.py` extracts both, plus the WooCommerce gallery.
4. **NocoDB sync.** Existing products are loaded once; new ones are inserted,
   seen ones are updated. A `PriceHistory` row is appended **only when price or
   stock changed** — so the table tracks real changes, not every scrape.
5. **Deactivation.** Products that vanished from the sitemap entirely are
   flagged `IsActive=false` (never deleted — keeps the historical record).

## Prerequisites

- A running **FlareSolverr** instance reachable from wherever the scraper runs.
  (We deploy one in Coolify; any Docker host works:
  `docker run -p 8191:8191 ghcr.io/flaresolverr/flaresolverr:latest`.)
- A self-hosted **NocoDB** instance.

## NocoDB setup (one time)

1. Sign in to the NocoDB instance and create a **base** (click *New Base*).
2. Copy the **base ID** — it's the `p...` id in the URL when the base is open.
3. Create an **API token**: account menu → *Tokens* → *Create*.

The `Products` and `PriceHistory` **tables are created automatically** by the
scraper (or by `python -m src.schema`). You don't build any columns by hand.

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env       # fill in NOCODB_*, FLARESOLVERR_URL
python -m src.schema       # create the tables (optional — the scraper does this too)
python -m src.scraper      # scrape
```

The first run takes a couple of minutes longer than subsequent ones because it
has to call FlareSolverr once. Re-runs within ~1 hour reuse the cached
`.cf-cookies.json`.

## Schedule on GitHub Actions

Add these to repo **Settings → Secrets → Actions**, then
`.github/workflows/scrape.yml` runs daily at 02:00 UTC (07:00 PKT) and can be
triggered manually:

- `NOCODB_API_TOKEN`
- `NOCODB_BASE_ID`
- `FLARESOLVERR_URL`

The CI runner gets a fresh public IP each run, so it always re-solves Cloudflare
(no cached cookies to invalidate). One solve per daily run is fine.

## Tables (created automatically)

**Products** — `Slug`, `WpPostId`, `URL`, `Title`, `Brand`, `Category`,
`CategoryPath`, `SKU`, `CurrentPrice`, `Currency`, `InStock`, `Availability`,
`ShortDescriptionText`, `ShortDescriptionHtml`, `DescriptionHtml`, `Attributes`,
`Images`, `FirstSeen`, `LastSeen`, `IsActive`

**PriceHistory** — `Slug`, `ScrapedAt`, `Price`, `Currency`, `InStock`,
`Availability`

Column definitions live in `src/schema.py` — and `ensure_schema` adds any
missing column to an existing table automatically, so adding to that file is
all it takes.
