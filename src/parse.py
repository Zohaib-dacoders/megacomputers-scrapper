"""Pure functions for extracting product data from zahcomputers.pk HTML.

zahcomputers is WordPress + WooCommerce + WoodMart, server-rendered behind
Cloudflare. Once Cloudflare is solved (see flaresolverr.py), plain HTML carries
everything we need — no headless browser required.

Data sources, in order of authority:
1. JSON-LD (Yoast SEO emits @graph arrays containing Product + BreadcrumbList).
   Stable across redesigns — prefer it where possible.
2. Body class for `postid-XXXXX` -> the WP post ID.
3. HTML via selectolax for the gallery, the short-description bullets, and
   the spec table inside #tab-description. The spec table has three observed
   shapes (flat 2-col; Notion-style multi-section; .model-information-table
   with intervening <header class=section-header>) — `_mine_spec_tables`
   handles all three.

`parse_product(html, url)` returns None if no Product JSON-LD is present, so
non-product URLs that slip past the sitemap heuristic are silently dropped.

No I/O — keep it that way so tests can pass canned HTML.
"""

import html
import json
import re
from urllib.parse import urlparse

from selectolax.parser import HTMLParser, Node

JSONLD_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)

BODY_POSTID_RE = re.compile(r'class="[^"]*\bpostid-(\d+)\b[^"]*"', re.IGNORECASE)

# Image URLs in WordPress get -<W>x<H> size suffixes; we want the original.
IMAGE_SIZE_SUFFIX_RE = re.compile(r"-(\d+)x(\d+)(?=\.(?:jpe?g|png|webp|gif)(?:\?|$))", re.IGNORECASE)

# Strip the marketing tail that Yoast appends to <h1>/<title>.
PRICE_IN_PK_TAIL_RE = re.compile(r"\s*[-–—|]?\s*Price in Pakistan(?:\s+Specs?)?\s*$", re.IGNORECASE)

# Spec-table noise rows we skip wholesale (case-insensitive, exact key match).
SPEC_TABLE_HEADER_KEYS = {"specification", "specifications", "feature", "features"}
SPEC_TABLE_HEADER_VALUES = {"value", "values", "details", "detail"}

# In Lenovo's spec table, single-cell-content rows like "DESIGN", "SOFTWARE",
# "CONNECTIVITY" are section labels (with second cell empty/&nbsp;). We detect
# them by the empty value cell rather than maintaining a list of section names.

SITEMAP_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.IGNORECASE)


# -------- URL / slug --------

def slug_from_url(url: str) -> str:
    """The last non-empty path segment. For /product/foo-bar/ -> 'foo-bar'."""
    path = urlparse(url).path.rstrip("/")
    return path.rsplit("/", 1)[-1] if path else url


def is_likely_product_url(url: str) -> bool:
    """zahcomputers product pages live under /product/<slug>/."""
    path = urlparse(url).path.strip("/")
    return path.startswith("product/") and path.count("/") == 1


def is_product_sitemap(url: str) -> bool:
    """The sitemap_index links to many sub-sitemaps; only product-sitemap*.xml
    carry product URLs (others are posts, pages, taxonomies)."""
    return "product-sitemap" in url.lower()


def parse_sitemap_urls(xml: str) -> list[str]:
    """Extract every <loc> URL. Works on the index AND on sub-sitemaps."""
    return SITEMAP_LOC_RE.findall(xml)


# -------- JSON-LD --------

def _extract_jsonld_blocks(html: str) -> list[dict]:
    """Flatten all JSON-LD payloads on the page into a list of dicts. Yoast
    wraps everything in @graph, so we recurse one level into that as well."""
    out: list[dict] = []
    for raw in JSONLD_RE.findall(html):
        try:
            data = json.loads(raw.strip())
        except json.JSONDecodeError:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            graph = item.get("@graph")
            if isinstance(graph, list):
                out.extend(g for g in graph if isinstance(g, dict))
            else:
                out.append(item)
    return out


def _find_by_type(blocks: list[dict], wanted: str) -> dict | None:
    for b in blocks:
        t = b.get("@type")
        if t == wanted or (isinstance(t, list) and wanted in t):
            return b
    return None


def _bc_names(bc: dict) -> list[str]:
    names: list[str] = []
    for it in bc.get("itemListElement") or []:
        name = it.get("name") or (it.get("item") or {}).get("name")
        if name:
            names.append(_unescape(name))
    return names


def _breadcrumb_trail(blocks: list[dict], tree: HTMLParser) -> list[str]:
    """Category trail between 'Home' and the product itself.

    Pages carry two BreadcrumbList JSON-LD entries: Yoast's generic 'Home > Shop
    > <product>' and the WC one with the real category ('Home > Monitor > ...').
    We collect both, prefer trails whose middle segments are NOT the generic
    'Shop', and pick the longest. If JSON-LD only gives 'Shop' (some legacy
    products are like this), fall back to WoodMart's <nav class=wd-breadcrumbs>
    widget."""
    candidates: list[list[str]] = []
    for b in blocks:
        t = b.get("@type")
        if t == "BreadcrumbList" or (isinstance(t, list) and "BreadcrumbList" in t):
            ns = _bc_names(b)
            if len(ns) >= 2:
                candidates.append(ns)

    def trim(trail: list[str]) -> list[str]:
        return trail[1:-1] if len(trail) > 2 else trail[1:]

    specific = [t for t in candidates if not _is_shop_only(trim(t))]
    if specific:
        return trim(max(specific, key=len))
    if candidates:
        # JSON-LD only knows 'Shop' — try the HTML breadcrumb widget instead.
        widget = _wd_breadcrumb_trail(tree)
        if widget:
            return widget
        return trim(candidates[-1])
    return _wd_breadcrumb_trail(tree)


def _is_shop_only(middle: list[str]) -> bool:
    return all(s.strip().lower() == "shop" for s in middle) if middle else True


def _wd_breadcrumb_trail(tree: HTMLParser) -> list[str]:
    """Extract category trail from <nav class=wd-breadcrumbs> — the rendered
    WoodMart widget. Each <a> is a node in the trail; the final non-link
    <span class=wd-last> is the product itself, which we drop."""
    nav = tree.css_first(".wd-breadcrumbs, .woocommerce-breadcrumb")
    if not nav:
        return []
    out = []
    for a in nav.css("a"):
        txt = a.text(strip=True)
        if txt and txt.lower() != "home":
            out.append(txt)
    return out


def _unescape(s: str) -> str:
    """Decode HTML entities. JSON-LD on this site is *double-escaped* in some
    fields (e.g. `&amp;#8211;` in breadcrumb names) — we unescape twice to
    flatten both layers. Idempotent on already-decoded text."""
    return html.unescape(html.unescape(s)).strip()


def _stringify(v) -> str | None:
    """Coerce a JSON-LD scalar to a clean trimmed string, or None if empty.
    Some products (e.g. Dell) emit `sku` as a number rather than a string."""
    if v is None or v == "":
        return None
    return str(v).strip() or None


def _norm_key(k: str) -> str:
    """Strip trailing colon / whitespace from a spec/attribute label."""
    return k.rstrip(":").rstrip("：").strip()


def _offers_price(offers: list | dict) -> tuple[float | None, str | None]:
    """Zahcomputers nests price under offers[0].priceSpecification[0].price.
    Some products may also expose a direct offers[0].price; try both."""
    if not offers:
        return None, None
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    if not isinstance(offers, dict):
        return None, None
    ps = offers.get("priceSpecification") or []
    if isinstance(ps, dict):
        ps = [ps]
    raw = None
    currency = None
    for spec in ps:
        if isinstance(spec, dict) and spec.get("price") not in (None, ""):
            raw = spec["price"]
            currency = spec.get("priceCurrency")
            break
    if raw is None:
        raw = offers.get("price")
        currency = offers.get("priceCurrency")
    try:
        price = float(raw) if raw not in (None, "") else None
    except (TypeError, ValueError):
        price = None
    return price, currency


def _availability_to_bool(av: str | None) -> bool | None:
    if not av:
        return None
    return "InStock" in av and "OutOfStock" not in av


# -------- HTML extraction --------

def _wp_post_id(html: str) -> int | None:
    m = BODY_POSTID_RE.search(html)
    return int(m.group(1)) if m else None


def _strip_image_size_suffix(url: str) -> str:
    """foo-580x435.jpg -> foo.jpg (so different-size variants dedupe)."""
    return IMAGE_SIZE_SUFFIX_RE.sub("", url)


def _extract_gallery_images(tree: HTMLParser) -> list[str]:
    """WoodMart's gallery widget links each <figure> to the original-size file.
    Ordered. Returns [] if no gallery (common on simple/old products)."""
    out: list[str] = []
    seen: set[str] = set()
    for a in tree.css(".woocommerce-product-gallery .wd-carousel-item figure > a"):
        href = a.attributes.get("href")
        if not href:
            continue
        canon = _strip_image_size_suffix(href)
        if canon not in seen:
            seen.add(canon)
            out.append(href)
    return out


def _extract_description_images(tree: HTMLParser) -> list[str]:
    """Inline product images embedded inside #tab-description. The srcset has
    every size variant; the <img src> is usually the largest. We canonicalise
    by stripping the -WxH suffix and de-duplicate."""
    out: list[str] = []
    seen: set[str] = set()
    panel = tree.css_first("#tab-description")
    if not panel:
        return out
    for img in panel.css("img"):
        src = img.attributes.get("src") or img.attributes.get("data-src")
        if not src:
            continue
        canon = _strip_image_size_suffix(src)
        if canon not in seen:
            seen.add(canon)
            out.append(canon)
    return out


def _merge_images(json_ld_image, gallery: list[str], description: list[str]) -> list[str]:
    """Order: gallery (most authoritative) -> description -> JSON-LD featured.
    De-duplicate by the size-stripped URL."""
    if json_ld_image is None:
        featured: list[str] = []
    elif isinstance(json_ld_image, str):
        featured = [json_ld_image]
    elif isinstance(json_ld_image, list):
        featured = [x for x in json_ld_image if isinstance(x, str)]
    else:
        featured = []

    out: list[str] = []
    seen: set[str] = set()
    for src in (*gallery, *description, *featured):
        canon = _strip_image_size_suffix(src)
        if canon not in seen:
            seen.add(canon)
            out.append(src)
    return out


def _parse_short_description(tree: HTMLParser) -> tuple[str | None, str | None]:
    """The WooCommerce short description (bullets, in this site's case).
    Returns (plain_text, raw_inner_html)."""
    node = tree.css_first(".woocommerce-product-details__short-description")
    if not node:
        return None, None
    text = node.text(separator=" ", strip=True) or None
    html = (node.html or "").strip() or None
    # Drop the wrapping <div>, keep the inner.
    if html and html.startswith("<div"):
        inner = re.sub(r"^<div[^>]*>", "", html)
        inner = re.sub(r"</div>\s*$", "", inner).strip()
        html = inner or None
    return text, html


def _parse_description_html(tree: HTMLParser) -> str | None:
    """The description tab MINUS its tables and section headers — usually near
    empty on zahcomputers (the tab is mostly spec tables). Returns None if
    there's no actual prose left."""
    panel = tree.css_first("#tab-description")
    if not panel:
        return None
    raw = panel.html or ""
    # Strip wrapping <div id="tab-description" ...>...</div>
    raw = re.sub(r"^<div[^>]*>", "", raw)
    raw = re.sub(r"</div>\s*$", "", raw)
    # Drop the table blocks and the section-header blocks.
    raw = re.sub(r"<table\b[^>]*>.*?</table>", "", raw, flags=re.DOTALL | re.IGNORECASE)
    raw = re.sub(r'<header[^>]*class="[^"]*section-header[^"]*"[^>]*>.*?</header>',
                 "", raw, flags=re.DOTALL | re.IGNORECASE)
    # Strip Notion's wrapper around the spec-table container, if present.
    raw = re.sub(r"<div[^>]*class=\"TyagGW_tableContainer\"[^>]*>\s*</div>", "", raw, flags=re.IGNORECASE)
    cleaned = raw.strip()
    return cleaned or None


def _parse_shop_attributes(tree: HTMLParser) -> dict[str, str]:
    """WooCommerce's "Additional Information" tab — a <th>/<td> table at
    .shop_attributes. Often absent on zahcomputers; specs live in the
    description tab instead. Strategy A of the merge."""
    out: dict[str, str] = {}
    table = tree.css_first(".shop_attributes")
    if not table:
        return out
    for row in table.css("tr"):
        label = row.css_first("th, .label")
        value = row.css_first("td, .value")
        if not (label and value):
            continue
        k = _norm_key(label.text(strip=True))
        v = value.text(separator=" | ", strip=True)
        if k and v:
            out[k] = v
    return out


def _cell_key(cell: Node) -> str:
    """Spec-cell key extraction. In `.model-information-table` (Dell shape) the
    key cell is `<td>Name<p></p><p>Description</p></td>` — we want just "Name".
    Take the cell's HTML, split on the first <p> or <br>, strip tags."""
    raw = cell.html or ""
    raw = re.sub(r"^<(?:td|th)[^>]*>", "", raw)
    raw = re.sub(r"</(?:td|th)>\s*$", "", raw)
    head = re.split(r"<\s*(?:p|br)\b", raw, maxsplit=1)[0]
    text = re.sub(r"<[^>]+>", " ", head)
    # th/td keys carry literal entities (&nbsp;) and NBSP before the colon —
    # decode and normalise so keys are clean ("CPU Socket Type", not
    # "CPU Socket Type&nbsp;").
    text = html.unescape(text).replace("\xa0", " ")
    return " ".join(text.split()).strip()


def _cell_value(cell: Node) -> str:
    """Spec-cell value extraction. Multi-line values (multi-unit measurements,
    feature lists separated by <br>) are joined with ' | '. NBSP becomes a
    real space."""
    text = cell.text(separator=" | ", strip=True)
    return text.replace("\xa0", " ").strip()


def _is_header_row(key: str, value: str) -> bool:
    """Spec tables sometimes embed their own header rows like 'Specification |
    Value' or 'Specification | Details' — skip those."""
    return key.lower() in SPEC_TABLE_HEADER_KEYS and value.lower() in SPEC_TABLE_HEADER_VALUES


def _row_key_value(row: Node) -> tuple[Node, Node] | None:
    """Pick the (key_cell, value_cell) from a spec row, across cell shapes:
      - two <td>          (KOORUI/Lenovo/Dell): key=td[0], value=td[1]
      - one <th> + one <td> (ASUS motherboard/PSU and most WooCommerce/WoodMart
        spec tables): key=th, value=td
      - two <th>          (some tables header both columns): key=th[0], value=th[1]
    Returns None for rows that aren't a 2-cell key/value pair (section markers,
    colspan banners, etc.)."""
    th = row.css("th")
    td = row.css("td")
    if not th and len(td) == 2:
        return td[0], td[1]
    if len(th) == 1 and len(td) == 1:
        return th[0], td[0]
    if len(th) == 2 and not td:
        return th[0], th[1]
    return None


def _spec_table_roots(tree: HTMLParser) -> list[Node]:
    """Container(s) to mine spec tables from. Normally `#tab-description`, but
    some products render their spec tables inside Elementor/WooCommerce tab
    panels (`.elementor-tab-content`, `.wc-tab`) with no `#tab-description` — or
    a `#tab-description` that's present but empty of tables. Fall back to those
    panels so those products (e.g. ASUS routers) aren't left spec-less.

    Prefer `#tab-description` when it actually carries tables, to avoid pulling
    in unrelated tab content on the common path."""
    panel = tree.css_first("#tab-description")
    if panel and panel.css("table"):
        return [panel]
    # Fallback: spec tables can live in the WoodMart single-product content
    # widget (.wd-single-content) or Elementor/WooCommerce tab panels rather
    # than #tab-description. Gather from all candidates; _mine_description_tables
    # de-dupes tables by identity and the 2-cell key/value filter keeps out
    # non-spec (layout/related) tables.
    roots: list[Node] = []
    for sel in (".wd-single-content", ".elementor-widget-wd_single_product_content",
                ".elementor-tab-content", ".wc-tab", ".woocommerce-Tabs-panel"):
        roots.extend(tree.css(sel))
    if roots:
        return roots
    return [panel] if panel else []


def _mine_description_tables(tree: HTMLParser) -> dict[str, str]:
    """Mine every spec <table> for 2-cell key/value rows. Handles the observed
    shapes: KOORUI plain 2-<td>, Lenovo Notion-style with section rows, Dell
    .model-information-table with intervening <header>s, and the common
    WooCommerce/WoodMart `<th>key</th><td>value</td>` table (ASUS motherboards,
    PSUs, GPUs, RAM — previously dropped, yielding 0 specs). Tables are sourced
    from `#tab-description` or, as a fallback, Elementor/WooCommerce tab panels
    (see `_spec_table_roots`).

    Strategy B of the merge. Returns a flat key->value dict; if a key recurs
    across tables (Dell has "Width" under both Display and Dimensions) the
    last write wins."""
    out: dict[str, str] = {}
    seen_tables: set[int] = set()
    tables: list[Node] = []
    for root in _spec_table_roots(tree):
        for table in root.css("table"):
            # nested roots (panel inside a tab panel) can surface the same table
            # twice — de-dupe by identity so we don't double-process.
            if id(table) in seen_tables:
                continue
            seen_tables.add(id(table))
            tables.append(table)
    for table in tables:
        for row in table.css("tr"):
            kv = _row_key_value(row)
            if kv is None:
                continue
            key = _norm_key(_cell_key(kv[0]))
            value = _cell_value(kv[1])
            if not key:
                continue
            if not value:
                # Lenovo's "DESIGN | (empty)" section markers — skip.
                continue
            if _is_header_row(key, value):
                continue
            out[key] = value
    return out


def _brand_from_product_meta(tree: HTMLParser) -> str | None:
    """WooCommerce's `product_brand` taxonomy renders as <a> under .product_meta
    (or sometimes a dedicated .product_brand span). zahcomputers doesn't seem
    to use it widely — present only on some products — so this is a fallback,
    not the primary brand source."""
    for sel in (".product_meta a[href*='/brand/']", ".product_meta .brand a", ".product_brand a"):
        node = tree.css_first(sel)
        if node:
            text = node.text(strip=True)
            if text:
                return text
    return None


# -------- main --------

def parse_product(html: str, url: str) -> dict | None:
    """Extract the product fields from a zahcomputers.pk product page.
    Returns None if there's no Product JSON-LD (e.g. the page is a category
    or 404). Never raises on missing optional fields."""
    blocks = _extract_jsonld_blocks(html)
    product = _find_by_type(blocks, "Product")
    if not product:
        return None

    tree = HTMLParser(html)
    title = _unescape(product.get("name") or "") or None
    if title:
        title = PRICE_IN_PK_TAIL_RE.sub("", title).strip() or None

    price, currency = _offers_price(product.get("offers"))
    offers = product.get("offers")
    if isinstance(offers, list):
        offer0 = offers[0] if offers else {}
    elif isinstance(offers, dict):
        offer0 = offers
    else:
        offer0 = {}
    availability = offer0.get("availability")

    trail = _breadcrumb_trail(blocks, tree)
    category = trail[-1] if trail else None
    category_path = " > ".join(trail) or None

    attributes = _parse_shop_attributes(tree)
    attributes.update(_mine_description_tables(tree))   # description tables win on overlap

    brand_jsonld = product.get("brand")
    if isinstance(brand_jsonld, dict):
        brand_jsonld = brand_jsonld.get("name")
    elif isinstance(brand_jsonld, list) and brand_jsonld:
        first = brand_jsonld[0]
        brand_jsonld = first.get("name") if isinstance(first, dict) else first
    brand = (attributes.get("Brand")
             or attributes.get("brand")
             or _brand_from_product_meta(tree)
             or brand_jsonld
             or None)

    short_text, short_html = _parse_short_description(tree)
    images = _merge_images(
        product.get("image"),
        _extract_gallery_images(tree),
        _extract_description_images(tree),
    )

    return {
        "slug": slug_from_url(url),
        "url": url,
        "wp_post_id": _wp_post_id(html),
        "title": title,
        "sku": _stringify(product.get("sku")),
        "brand": brand,
        "category": category,
        "category_path": category_path,
        "price": price,
        "currency": currency or "PKR",
        "availability": availability,
        "in_stock": _availability_to_bool(availability),
        "short_description_text": short_text,
        "short_description_html": short_html,
        "description_html": _parse_description_html(tree),
        "attributes": attributes,
        "images": images,
    }
