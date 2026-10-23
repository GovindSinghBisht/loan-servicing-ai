"""PostgreSQL setup: a dedicated database, the schema, and a least-privilege app user.

    uv run python -m engine.db_setup

Admin access comes from .env, never the command line: DATABASE_ADMIN_URL, or DB_HOST, DB_PORT (5432),
DB_ADMIN_USER, DB_ADMIN_PASSWORD, DB_ADMIN_DATABASE (postgres).
Optional: DB_NAME (blue_machines), DB_APP_USER (blue_machines_app).

What it does (safe to re-run; the app password is rotated each run):
- creates the database if missing and the schema as the admin, so the admin owns every table;
- creates the app user with only what the code needs: read/write on conversation tables, retention
  deletes, and append-only (SELECT, INSERT) on audit, guardrail and voice tables, so even a compromised
  app cannot rewrite the audit trail; no CREATE / DROP / ALTER anywhere;
- revokes CONNECT from PUBLIC so other roles on the server cannot open this database;
- writes DATABASE_URL for the app user into .env.
"""
from __future__ import annotations

import asyncio
import os
import re
import secrets
import sys
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

import asyncpg
from dotenv import load_dotenv

from .storage import Store, metadata

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
IDENT = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

# Table -> privileges the application needs. Anything not listed is denied.
GRANTS = {
    "conversations": "SELECT, INSERT, UPDATE, DELETE",
    "messages": "SELECT, INSERT, DELETE",
    "tool_calls": "SELECT, INSERT, DELETE",
    "turn_metrics": "SELECT, INSERT, DELETE",
    "audit_events": "SELECT, INSERT",       # append-only
    "guardrail_events": "SELECT, INSERT",   # append-only
    "voice_metrics": "SELECT, INSERT",
    "auth_sessions": "SELECT, INSERT, UPDATE",  # revoke = update; rows are kept for audit
}


def _admin() -> dict:
    if url := os.environ.get("DATABASE_ADMIN_URL"):
        u = urlsplit(url.replace("+asyncpg", "").replace("+psycopg", ""))
        return {"host": u.hostname, "port": u.port or 5432, "user": unquote(u.username or ""),
                "password": unquote(u.password or ""), "database": "postgres"}
    missing = [k for k in ("DB_HOST", "DB_ADMIN_USER", "DB_ADMIN_PASSWORD") if not os.environ.get(k)]
    if missing:
        sys.exit(f"Missing {', '.join(missing)} (or DATABASE_ADMIN_URL) in .env")
    return {"host": os.environ["DB_HOST"], "port": int(os.environ.get("DB_PORT", "5432")),
            "user": os.environ["DB_ADMIN_USER"], "password": os.environ["DB_ADMIN_PASSWORD"],
            "database": os.environ.get("DB_ADMIN_DATABASE", "postgres")}


def _set_env(key: str, value: str) -> None:
    lines = ENV_FILE.read_text().splitlines() if ENV_FILE.exists() else []
    lines = [line for line in lines if not line.startswith(f"{key}=")] + [f"{key}={value}"]
    ENV_FILE.write_text("\n".join(lines) + "\n")


async def main() -> None:
    load_dotenv(ENV_FILE)
    admin = _admin()
    db = os.environ.get("DB_NAME", "blue_machines")
    app_user = os.environ.get("DB_APP_USER", "blue_machines_app")
    for ident in (db, app_user):
        if not IDENT.match(ident):
            sys.exit(f"Invalid identifier {ident!r}")
    app_pw = secrets.token_urlsafe(32)
    connect = dict(host=admin["host"], port=admin["port"], user=admin["user"], password=admin["password"],
                   ssl="require", timeout=15)

    print(f"Connecting to {admin['host']} as {admin['user']} ...")
    conn = await asyncpg.connect(database=admin["database"], **connect)
    try:
        version = await conn.fetchval("show server_version")
        if not await conn.fetchval("select 1 from pg_database where datname = $1", db):
            await conn.execute(f'create database "{db}"')
            print(f"  database {db}: created")
        else:
            print(f"  database {db}: exists")
        exists = await conn.fetchval("select 1 from pg_roles where rolname = $1", app_user)
        if exists:  # RDS masters are not superusers: on an existing role only the password can be changed
            await conn.execute(f'alter role "{app_user}" with login password $${app_pw}$$')
        else:
            await conn.execute(f'create role "{app_user}" with login nosuperuser nocreatedb nocreaterole noinherit '
                               f'password $${app_pw}$$')
        print(f"  role {app_user}: {'exists, password rotated' if exists else 'created'}")
        await conn.execute(f'revoke connect on database "{db}" from public')
        await conn.execute(f'grant connect on database "{db}" to "{app_user}"')
    finally:
        await conn.close()

    # Schema is created by the admin, so the admin (not the app) owns every table.
    admin_url = (f"postgresql+asyncpg://{quote(admin['user'], safe='')}:{quote(admin['password'], safe='')}"
                 f"@{admin['host']}:{admin['port']}/{db}?ssl=require")
    store = Store(admin_url)
    await store.init()
    await store.close()

    conn = await asyncpg.connect(database=db, **connect)
    try:
        await conn.execute("revoke create on schema public from public")
        await conn.execute(f'grant usage on schema public to "{app_user}"')
        await conn.execute(f'revoke all on all tables in schema public from "{app_user}"')
        for table, privileges in GRANTS.items():
            await conn.execute(f'grant {privileges} on table "{table}" to "{app_user}"')
        await conn.execute(f'grant usage, select on all sequences in schema public to "{app_user}"')
    finally:
        await conn.close()

    _set_env("DATABASE_URL", f"postgresql+asyncpg://{app_user}:{app_pw}@{admin['host']}:{admin['port']}/{db}?ssl=require")
    print(f"  schema: {len(metadata.sorted_tables)} tables, owned by {admin['user']}")
    for table, privileges in GRANTS.items():
        print(f"    {table:<17} {privileges}")
    print(f"Done. PostgreSQL {version}. DATABASE_URL now uses {app_user}.")
    print("Next: remove admin credentials (DB_ADMIN_PASSWORD / DATABASE_ADMIN_URL) from .env and restart the API.")


if __name__ == "__main__":
    asyncio.run(main())
