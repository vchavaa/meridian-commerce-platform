import json
import os
import threading
import time

import psycopg

from common.database import connect

_CACHE_TTL_SECONDS = 2.0
_cache = {}
_cache_lock = threading.Lock()

# When the control-store is unreachable and we have no cached value, choose
# between fail-closed (treat as drained; readiness returns 503) and the legacy
# fail-open behavior. Fail-closed is safer: a service that cannot see its drain
# state should remove itself from rotation rather than silently serve traffic.
_FAIL_CLOSED_ON_ERROR = os.environ.get("REGION_STATE_FAIL_CLOSED", "true").lower() == "true"


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
        previous = cached[1] if cached else None
    try:
        with connect() as connection:
            row = connection.execute(
                "SELECT drained FROM regional_controls WHERE region = %s",
                (region,),
            ).fetchone()
    except psycopg.Error as exc:
        # Prefer the last cached value (bounded staleness) over fail-closed to
        # avoid flapping on transient blips. If there is NO cached value, fail
        # closed by default - the service cannot vouch for its drain state.
        if previous is not None:
            effective = previous
            reason = "using last cached value"
        elif _FAIL_CLOSED_ON_ERROR:
            effective = True
            reason = "no cache; failing closed (readiness=503)"
        else:
            effective = fallback
            reason = f"no cache; using static fallback={fallback}"
        print(json.dumps({
            "severity": "ERROR",
            "message": f"Could not refresh regional drain state; {reason}",
            "region": region,
            "database_error": type(exc).__name__,
            "effective_drained": effective,
        }), flush=True)
        with _cache_lock:
            _cache[region] = (now, effective)
        return effective
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
