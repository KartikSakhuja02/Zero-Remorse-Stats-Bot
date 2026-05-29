from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

import psycopg
from psycopg.rows import dict_row

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS player_stats (
    player_name TEXT PRIMARY KEY,
    matches INTEGER NOT NULL DEFAULT 0,
    mvp INTEGER NOT NULL DEFAULT 0,
    kills INTEGER NOT NULL DEFAULT 0,
    kill_per_match NUMERIC(10, 2) GENERATED ALWAYS AS (
        CASE
            WHEN matches = 0 THEN 0
            ELSE ROUND(kills::numeric / matches, 2)
        END
    ) STORED,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


@dataclass
class Database:
    database_url: str

    def __post_init__(self) -> None:
        self._initialize()

    def _connect(self) -> psycopg.Connection:
        connection = psycopg.connect(self.database_url, row_factory=dict_row)
        connection.autocommit = False
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(SCHEMA_SQL)
            connection.commit()

    @contextmanager
    def connect(self) -> Iterator[psycopg.Connection]:
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
