"""Alembic for schema `rec` only. The version table lives in `rec` too."""

from alembic import context
from sqlalchemy import create_engine, text

from rec_engine.config import settings

SCHEMA = "rec"


def run_migrations_online() -> None:
    url = context.config.attributes.get("url") or settings().sqlalchemy_url
    engine = create_engine(url)
    with engine.begin() as connection:
        # In the real stack infra/postgres/init creates `rec` owned by rec_svc; this only
        # matters for a fresh development or test database.
        exists = connection.execute(
            text("SELECT 1 FROM pg_namespace WHERE nspname = :s"), {"s": SCHEMA}
        ).first()
        if exists is None:
            connection.execute(text(f"CREATE SCHEMA {SCHEMA}"))
        context.configure(
            connection=connection,
            version_table_schema=SCHEMA,
            include_schemas=False,
        )
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
