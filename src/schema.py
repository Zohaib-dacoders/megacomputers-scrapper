"""NocoDB table bootstrap.

The scraper needs a `Products` table and a `PriceHistory` table. Rather than
have the user build ~21 columns by hand in the NocoDB UI, `ensure_schema()`
creates whatever is missing inside a base the user has already made.

The *base* is not created here: base creation on NocoDB is workspace-bound
and version-sensitive, and making one is a single click in the UI. The user
supplies `NOCODB_BASE_ID`; every table inside it is handled automatically.

Standalone use (create tables without scraping):  python -m src.schema
"""

import logging
import os
import sys

from dotenv import load_dotenv

from .nocodb import NocoDB

load_dotenv()

log = logging.getLogger("zah-scraper.schema")

# (column name, NocoDB uidt). The first column becomes the table's display value.
PRODUCTS_COLUMNS: list[tuple[str, str]] = [
    ("Slug", "SingleLineText"),           # the display column — last segment of /product/SLUG/
    ("WpPostId", "Number"),               # WP post id from body class postid-XXXXX
    ("URL", "URL"),
    ("Title", "SingleLineText"),
    ("Brand", "SingleLineText"),
    ("Category", "SingleLineText"),       # most-specific breadcrumb segment
    ("CategoryPath", "SingleLineText"),   # full breadcrumb, e.g. "Laptop > New Laptop"
    ("SKU", "SingleLineText"),
    ("CurrentPrice", "Decimal"),
    ("Currency", "SingleLineText"),
    ("InStock", "Checkbox"),
    ("Availability", "SingleLineText"),   # raw schema.org availability URL
    ("ShortDescriptionText", "LongText"), # plain-text bullets / summary
    ("ShortDescriptionHtml", "LongText"), # the bulleted HTML, preserved for re-publishing
    ("DescriptionHtml", "LongText"),      # description-tab prose minus tables/headers (often near-empty)
    ("Attributes", "LongText"),           # JSON of {key: value} from the spec tables
    ("Images", "LongText"),               # JSON array of original-size image URLs
    ("FirstSeen", "DateTime"),
    ("LastSeen", "DateTime"),
    ("IsActive", "Checkbox"),
]

PRICE_HISTORY_COLUMNS: list[tuple[str, str]] = [
    ("Slug", "SingleLineText"),
    ("ScrapedAt", "DateTime"),
    ("Price", "Decimal"),
    ("Currency", "SingleLineText"),
    ("InStock", "Checkbox"),
    ("Availability", "SingleLineText"),
]

TABLES: dict[str, list[tuple[str, str]]] = {
    "Products": PRODUCTS_COLUMNS,
    "PriceHistory": PRICE_HISTORY_COLUMNS,
}


def ensure_schema(client: NocoDB, base_id: str) -> dict[str, str]:
    """Return {table_title: table_id}, creating any missing table and adding any
    missing column to a table that already exists.

    Reconciliation is additive only — it never renames, retypes, or drops a
    column. To change an existing column, edit it in the NocoDB UI.
    """
    existing = {t["title"]: t["id"] for t in client.list_tables(base_id)}
    resolved: dict[str, str] = {}
    for title, columns in TABLES.items():
        if title in existing:
            table_id = existing[title]
            resolved[title] = table_id
            log.info("table %r already exists (%s)", title, table_id)
            have = set(client.list_columns(table_id))
            for name, uidt in columns:
                if name not in have:
                    client.create_column(table_id, name, uidt)
                    log.info("  + added missing column %r (%s)", name, uidt)
        else:
            table_id = client.create_table(base_id, title, columns)
            resolved[title] = table_id
            log.info("created table %r (%s)", title, table_id)
    return resolved


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    base_id = os.getenv("NOCODB_BASE_ID", "")
    if not base_id:
        log.error("NOCODB_BASE_ID must be set")
        sys.exit(2)
    with NocoDB() as client:
        tables = ensure_schema(client, base_id)
    print("Resolved tables:")
    for title, table_id in tables.items():
        print(f"  {title}: {table_id}")


if __name__ == "__main__":
    main()
