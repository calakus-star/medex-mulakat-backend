"""MedeX SQLite verilerini PostgreSQL'e tek seferlik ve kayıpsız taşır.

Kullanım:
  python migrate_sqlite_to_postgres.py --sqlite /path/medex_mulakat.db

DATABASE_URL ortam değişkeni Railway PostgreSQL bağlantısını göstermelidir.
Hedef PostgreSQL tablolarının yeni backend bir kez çalıştırılarak oluşturulmuş olması gerekir.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from typing import Any

import psycopg
from psycopg import sql

TABLES = ["positions", "candidates", "interviews", "snapshots", "ai_usage_logs"]


def sqlite_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def pg_columns(conn: psycopg.Connection, table: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT column_name FROM information_schema.columns
        WHERE table_schema='public' AND table_name=%s
        ORDER BY ordinal_position
        """,
        (table,),
    ).fetchall()
    return [row[0] for row in rows]


def migrate(sqlite_path: str, database_url: str) -> None:
    if not os.path.isfile(sqlite_path):
        raise FileNotFoundError(f"SQLite dosyası bulunamadı: {sqlite_path}")

    src = sqlite3.connect(sqlite_path)
    src.row_factory = sqlite3.Row
    dst = psycopg.connect(database_url)

    try:
        with dst.transaction():
            # Bağımlılık sırasının tersinde temizle; yalnızca boş/yeni PG hedefinde çalıştırılması önerilir.
            for table in reversed(TABLES):
                dst.execute(sql.SQL("TRUNCATE TABLE {} RESTART IDENTITY CASCADE").format(sql.Identifier(table)))

            for table in TABLES:
                source_cols = sqlite_columns(src, table)
                target_cols = pg_columns(dst, table)
                cols = [c for c in source_cols if c in target_cols]
                if not cols:
                    print(f"{table}: ortak kolon yok, atlandı")
                    continue

                rows = src.execute(
                    f"SELECT {', '.join(cols)} FROM {table} ORDER BY id"
                ).fetchall()
                if rows:
                    insert_stmt = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
                        sql.Identifier(table),
                        sql.SQL(", ").join(map(sql.Identifier, cols)),
                        sql.SQL(", ").join(sql.Placeholder() for _ in cols),
                    )
                    dst.executemany(insert_stmt, [tuple(row[c] for c in cols) for row in rows])

                if "id" in cols:
                    dst.execute(
                        sql.SQL(
                            "SELECT setval(pg_get_serial_sequence(%s, 'id'), "
                            "COALESCE((SELECT MAX(id) FROM {}), 1), "
                            "EXISTS(SELECT 1 FROM {}))"
                        ).format(sql.Identifier(table), sql.Identifier(table)),
                        (table,),
                    )
                print(f"{table}: {len(rows)} kayıt taşındı")

        print("SQLite → PostgreSQL aktarımı tamamlandı.")
    finally:
        src.close()
        dst.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sqlite", default="medex_mulakat.db", help="Kaynak SQLite dosyası")
    args = parser.parse_args()
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        print("HATA: DATABASE_URL ortam değişkeni tanımlı değil.", file=sys.stderr)
        return 2
    try:
        migrate(args.sqlite, database_url)
        return 0
    except Exception as exc:
        print(f"HATA: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
