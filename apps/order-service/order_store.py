import json
import psycopg
from psycopg.types.json import Jsonb

from common.database import connect


def initialize():
    with connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                order_id TEXT PRIMARY KEY,
                idempotency_key TEXT NOT NULL UNIQUE,
                request_hash TEXT NOT NULL,
                sku TEXT NOT NULL,
                quantity INTEGER NOT NULL CHECK (quantity > 0),
                status TEXT NOT NULL,
                processed_by_cluster TEXT NOT NULL,
                processed_by_region TEXT NOT NULL,
                processed_by_pod TEXT NOT NULL,
                release_track TEXT NOT NULL,
                true_client_ip TEXT,
                trace_id TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS order_outbox (
                event_id TEXT PRIMARY KEY,
                aggregate_id TEXT NOT NULL REFERENCES orders(order_id),
                event_type TEXT NOT NULL,
                payload JSONB NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                claimed_until TIMESTAMPTZ,
                published_at TIMESTAMPTZ
            )
            """
        )
        connection.execute(
            "ALTER TABLE order_outbox ADD COLUMN IF NOT EXISTS claimed_until TIMESTAMPTZ"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS orders_created_at_idx ON orders (created_at DESC)"
        )


def list_recent(limit=20):
    with connect() as connection:
        rows = connection.execute(
            """
            SELECT order_id, sku, quantity, status, processed_by_cluster,
                   processed_by_region, processed_by_pod, release_track,
                   true_client_ip, trace_id, created_at
            FROM orders
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (limit,),
        ).fetchall()
    return [
        {
            **row,
            "order_id": row["order_id"],
            "created_at": row["created_at"].isoformat(),
        }
        for row in rows
    ]


def create_order(order, idempotency_key, request_hash, event_id):
    with connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO orders (
                    order_id, idempotency_key, request_hash, sku, quantity,
                    status, processed_by_cluster, processed_by_region,
                    processed_by_pod, release_track, true_client_ip, trace_id
                )
                VALUES (
                    %(order_id)s, %(idempotency_key)s, %(request_hash)s,
                    %(sku)s, %(quantity)s, %(status)s,
                    %(processed_by_cluster)s, %(processed_by_region)s,
                    %(processed_by_pod)s, %(release_track)s, %(true_client_ip)s,
                    %(trace_id)s
                )
                ON CONFLICT (idempotency_key) DO NOTHING
                RETURNING order_id
                """,
                {**order, "idempotency_key": idempotency_key, "request_hash": request_hash},
            )
            inserted = cursor.fetchone()
            if inserted is None:
                cursor.execute(
                    "SELECT request_hash FROM orders WHERE idempotency_key = %s",
                    (idempotency_key,),
                )
                existing = cursor.fetchone()
                if existing is None or existing["request_hash"] != request_hash:
                    return "conflict", None
                cursor.execute(
                    "SELECT * FROM orders WHERE idempotency_key = %s",
                    (idempotency_key,),
                )
                return "existing", cursor.fetchone()

            cursor.execute(
                """
                INSERT INTO order_outbox (event_id, aggregate_id, event_type, payload)
                VALUES (%s, %s, %s, %s)
                """,
                (event_id, order["order_id"], "order.confirmed", Jsonb(order)),
            )
            cursor.execute(
                "SELECT * FROM orders WHERE order_id = %s",
                (order["order_id"],),
            )
            return "created", cursor.fetchone()


def serialize_order(row):
    if row is None:
        return None
    result = dict(row)
    result["created_at"] = result["created_at"].isoformat()
    result.pop("request_hash", None)
    result.pop("idempotency_key", None)
    return result


def claim_outbox_event():
    with connect() as connection:
        return connection.execute(
            """
            WITH next_event AS (
                SELECT event_id
                FROM order_outbox
                WHERE published_at IS NULL
                  AND (claimed_until IS NULL OR claimed_until < now())
                ORDER BY created_at
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            UPDATE order_outbox AS outbox
            SET claimed_until = now() + interval '60 seconds'
            FROM next_event
            WHERE outbox.event_id = next_event.event_id
            RETURNING outbox.event_id, outbox.payload
            """
        ).fetchone()


def mark_outbox_published(event_id):
    with connect() as connection:
        connection.execute(
            """
            UPDATE order_outbox
            SET published_at = now(), claimed_until = NULL
            WHERE event_id = %s
            """,
            (event_id,),
        )


def release_outbox_event(event_id):
    with connect() as connection:
        connection.execute(
            """
            UPDATE order_outbox
            SET claimed_until = now() + interval '5 seconds'
            WHERE event_id = %s AND published_at IS NULL
            """,
            (event_id,),
        )
