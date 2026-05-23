"""Cloudflare bypass via FlareSolverr.

zahcomputers.pk sits behind Cloudflare's JS challenge — plain httpx hits a
403 "Just a moment..." page. FlareSolverr (a Dockerised undetected-chromedriver)
solves the challenge once; we cache the resulting `cf_clearance` cookie and the
matching user-agent and reuse them on every subsequent httpx request until they
expire (~30 min to 2 h, then a fetch gets 403/503 and the scraper invalidates).

The cookie is bound to (cookie value, user-agent, IP). Send all three together;
if the IP changes (local dev vs. CI runner) the cookie is useless — call
`invalidate()` first.

Sync only. The scraper hops into a thread for these calls.
"""

import json
import logging
import os
import time
from dataclasses import dataclass

import httpx

log = logging.getLogger("zah-scraper.flaresolverr")


@dataclass
class CloudflareSession:
    """A reusable Cloudflare-cleared session: cookies + matching user-agent.

    Persists to a JSON file so re-runs within the same hour skip the solve."""

    flaresolverr_url: str
    cache_path: str = ".cf-cookies.json"
    session_ttl_seconds: int = 3600   # be safe: re-solve hourly even if cookie reports longer
    request_timeout_ms: int = 60000

    def get(self, target_url: str, force_refresh: bool = False) -> tuple[dict[str, str], str]:
        """Return (cookies, user_agent). Loads from cache if fresh, else solves."""
        if not force_refresh:
            cached = self._load()
            if cached:
                return cached["cookies"], cached["user_agent"]
        return self._solve_and_store(target_url)

    def invalidate(self) -> None:
        """Drop the cache so the next `get()` re-solves. Call this on 403/503."""
        try:
            os.unlink(self.cache_path)
            log.info("invalidated cookie cache at %s", self.cache_path)
        except FileNotFoundError:
            pass

    def _load(self) -> dict | None:
        try:
            with open(self.cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return None
        age = time.time() - data.get("solved_at", 0)
        if age > self.session_ttl_seconds:
            log.info("cookie cache %ds old (> %ds TTL); will re-solve", int(age), self.session_ttl_seconds)
            return None
        return data

    def _solve_and_store(self, target_url: str) -> tuple[dict[str, str], str]:
        log.info("solving Cloudflare challenge via FlareSolverr for %s", target_url)
        cookies, user_agent = _solve(self.flaresolverr_url, target_url, self.request_timeout_ms)
        payload = {
            "solved_at": time.time(),
            "target_url": target_url,
            "cookies": cookies,
            "user_agent": user_agent,
        }
        tmp = self.cache_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, self.cache_path)
        log.info("stored %d cookies + UA at %s", len(cookies), self.cache_path)
        return cookies, user_agent


class FlareSolverrError(RuntimeError):
    pass


def _solve(flaresolverr_url: str, target_url: str, timeout_ms: int) -> tuple[dict[str, str], str]:
    """Low-level: ask FlareSolverr to fetch a URL through Chrome and return cookies+UA.

    Returns (cookies_dict, user_agent). Raises on any failure — the caller decides
    whether to retry. The FlareSolverr HTTP timeout is `timeout_ms / 1000 + 30s`
    so the local httpx call always outlives the in-browser solve.
    """
    endpoint = flaresolverr_url.rstrip("/") + "/v1"
    body = {"cmd": "request.get", "url": target_url, "maxTimeout": timeout_ms}
    http_timeout = timeout_ms / 1000 + 30
    try:
        r = httpx.post(endpoint, json=body, timeout=http_timeout)
        r.raise_for_status()
        data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        raise FlareSolverrError(f"FlareSolverr call failed: {e}") from e

    if data.get("status") != "ok":
        raise FlareSolverrError(f"FlareSolverr did not solve: {data.get('message') or data}")

    sol = data.get("solution") or {}
    if sol.get("status") and sol["status"] >= 400:
        raise FlareSolverrError(
            f"FlareSolverr fetched {target_url} but upstream returned {sol['status']}"
        )

    cookies = {c["name"]: c["value"] for c in sol.get("cookies") or [] if c.get("name")}
    user_agent = sol.get("userAgent") or ""
    if "cf_clearance" not in cookies:
        log.warning("FlareSolverr returned no cf_clearance cookie — Cloudflare may not have served a challenge")
    if not user_agent:
        raise FlareSolverrError("FlareSolverr response missing userAgent")
    return cookies, user_agent
