"""Data layer — Postgres-backed (Way 1 hard cutover, 2026-06-22).

Was a NocoDB REST client; the scraper now writes directly to `pricesync-postgres`
(same instance/tables as the dashboard, base-prefixed `zah_*`). The `NocoDB` /
`NocoDBError` names are kept so scraper.py + schema.py need no changes. The
original REST client is preserved in `nocodb_rest.py`.
"""
from .pgdb import PgDB as NocoDB, NocoDBError  # noqa: F401
