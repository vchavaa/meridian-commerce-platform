import json
import threading
import time

import psycopg

from common.database import connect

_CACHE_TTL_SECONDS = 2.0
_cache = {}
_cache_lock = threading.Lock()


def initialize():
    with connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS regional_controls (
                region TEXT PRIMARY KEY,
                drained BOOLEAN NOT NULL DEFAULT FALSE,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )


def is_drained(region, fallback=False):
    now = time.monotonic()
    with _cache_lock:
        cached = _cache.get(region)
        if cached and now - cached[0] < _CACHE_TTL_SECONDS:
            return cached[1]
        previous = cached[1] if cached else fallback
    try:
        with connect() as connection:
            row = connection.execute(
                "SELECT drained FROM regional_controls WHERE region = %s",
                (region,),
            ).fetchone()
    except psycopg.Error as exc:
        print(json.dumps({
            "severity": "ERROR",
            "message": "Could not refresh regional drain state; using last known value",
            "region": region,
            "database_error": type(exc).__name__,
        }), flush=True)
        with _cache_lock:
            _cache[region] = (now, previous)
        return previous
    drained = row["drained"] if row else fallback
    with _cache_lock:
        _cache[region] = (now, drained)
    return drained


def set_drained(region, drained):
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO regional_controls (region, drained, updated_at)
            VALUES (%s, %s, now())
            ON CONFLICT (region)
            DO UPDATE SET drained = EXCLUDED.drained, updated_at = now()
            """,
            (region, drained),
        )
    with _cache_lock:
        _cache[region] = (time.monotonic(), drained)
