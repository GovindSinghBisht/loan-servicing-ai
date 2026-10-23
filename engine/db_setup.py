"""One-time PostgreSQL setup: a dedicated database and a least-privilege app user, then the schema.

    uv run python -m engine.db_setup

Reads admin credentials from .env (never from the command line, so they stay out of shell history):
    DB_HOST, DB_PORT (5432), DB_ADMIN_USER, DB_ADMIN_PASSWORD, DB_ADMIN_DATABASE (postgres)
Optional: DB_NAME (loan_servicing), DB_APP_USER (loan_app)

It creates the app user with a generated password, creates the database owned by that user, blocks
other roles from connecting, writes DATABASE_URL for the app user into .env and creates the tables.
Safe to re-run (the app password is rotated each run). Remove DB_ADMIN_PASSWORD from .env afterwards.
"""
from __future__ import annotations

import asyncio
import os
import re
import secrets
import sys
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

from .storage import Store, metadata

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
IDENT = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        sys.exit(f"Missing {name} in .env")
    return value


def _set_env(key: str, value: str) -> None:
    lines = ENV_FILE.read_text().splitlines() if ENV_FILE.exists() else []
    lines = [line for line in lines if not line.startswith(f"{key}=")] + [f"{key}={value}"]
    ENV_FILE.write_text("\n".join(lines) + "\n")


async def main() -> None:
    load_dotenv(ENV_FILE)
    host, port = _require("DB_HOST"), int(os.environ.get("DB_PORT", "5432"))
    admin, admin_pw = _require("DB_ADMIN_USER"), _require("DB_ADMIN_PASSWORD")
    admin_db = os.environ.get("DB_ADMIN_DATABASE", "postgres")
    db, app_user = os.environ.get("DB_NAME", "loan_servicing"), os.environ.get("DB_APP_USER", "loan_app")
    for ident in (db, app_user):
        if not IDENT.match(ident):
            sys.exit(f"Invalid identifier {ident!r}")
    app_pw = secrets.token_urlsafe(32)

    print(f"Connecting to {host}:{port} as admin ...")
    conn = await asyncpg.connect(host=host, port=port, user=admin, password=admin_pw, database=admin_db,
                                 ssl="require", timeout=15)
    try:
        version = await conn.fetchval("show server_version")
        if await conn.fetchval("select 1 from pg_roles where rolname = $1", app_user):
            await conn.execute(f'alter role "{app_user}" with login password $${app_pw}$$')
            print(f"  role {app_user}: exists, password rotated")
        else:
            await conn.execute(f'create role "{app_user}" with login password $${app_pw}$$')
            print(f"  role {app_user}: created")
        await conn.execute(f'grant "{app_user}" to current_user')  # lets the admin assign ownership on RDS
        if await conn.fetchval("select 1 from pg_database where datname = $1", db):
            print(f"  database {db}: exists")
        else:
            await conn.execute(f'create database "{db}" owner "{app_user}"')
            print(f"  database {db}: created (owner {app_user})")
        await conn.execute(f'revoke connect on database "{db}" from public')
        await conn.execute(f'grant connect on database "{db}" to "{app_user}"')
    finally:
        await conn.close()

    url = f"postgresql+asyncpg://{app_user}:{app_pw}@{host}:{port}/{db}?ssl=require"
    store = Store(url)
    await store.init()
    await store.close()
    _set_env("DATABASE_URL", url)
    print(f"  schema: {len(metadata.sorted_tables)} tables ready ({', '.join(t.name for t in metadata.sorted_tables)})")
    print(f"Done. PostgreSQL {version}. DATABASE_URL for {app_user} written to .env.")
    print("Next: remove DB_ADMIN_PASSWORD from .env, then restart the API.")


if __name__ == "__main__":
    asyncio.run(main())
