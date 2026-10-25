import json
import os
import time

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolTimeout

from common.secret_config import get_secret_value


_pool = None


def get_pool():
    global _pool
    if _pool is None:
        max_size = int(os.environ.get("DB_POOL_MAX_SIZE", "4"))
        if max_size < 1:
            raise ValueError("DB_POOL_MAX_SIZE must be at least 1")
        _pool = ConnectionPool(
            conninfo="",
            min_size=1,
            max_size=max_size,
            timeout=3,
            kwargs={
                "host": os.environ.get("DB_HOST", "localhost"),
                "port": int(os.environ.get("DB_PORT", "5432")),
                "dbname": os.environ.get("DB_NAME", "meridian"),
                "user": os.environ.get("DB_USER", "meridian"),
                "password": get_secret_value("DB_PASSWORD"),
                "connect_timeout": 3,
                "row_factory": dict_row,
            },
            open=True,
        )
    return _pool


def wait_for_database(timeout_seconds=120):
    pool = get_pool()
    deadline = time.monotonic() + timeout_seconds
    last_error = None
    while time.monotonic() < deadline:
        try:
            with pool.connection(timeout=3):
                return
        except (psycopg.Error, PoolTimeout) as exc:
            last_error = exc
            print(json.dumps({
                "severity": "WARNING",
                "message": "Waiting for PostgreSQL connection",
                "database_error": type(exc).__name__,
            }), flush=True)
            time.sleep(3)
    raise RuntimeError("PostgreSQL did not become available before startup timeout") from last_error


def connect():
    return get_pool().connection(timeout=3)
