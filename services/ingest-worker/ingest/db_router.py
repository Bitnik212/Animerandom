"""Send the catalog app to the `catalog` alias and everything else to `default` (schema ingest)."""

from typing import Any

from django.conf import settings
from django.db import connections
from django.db.models.signals import pre_migrate
from django.dispatch import receiver

CATALOG_APP = "catalog"
CATALOG_DB = "catalog"


class CatalogRouter:
    def db_for_read(self, model: Any, **hints: Any) -> str:
        return CATALOG_DB if model._meta.app_label == CATALOG_APP else "default"

    db_for_write = db_for_read

    def allow_relation(self, obj1: Any, obj2: Any, **hints: Any) -> bool:
        return self.db_for_read(type(obj1)) == self.db_for_read(type(obj2))

    def allow_migrate(self, db: str, app_label: str, **hints: Any) -> bool:
        return (app_label == CATALOG_APP) == (db == CATALOG_DB)


@receiver(pre_migrate)
def ensure_schema(sender: Any, using: str = "default", **kwargs: Any) -> None:
    """Create the alias's schema when missing (fresh dev or test databases).

    In the real stack `infra/postgres/init` creates both schemas and ingest_svc
    has no CREATE on the database, so this only checks and returns.
    """
    schema = settings.DB_SCHEMAS.get(using)
    if schema is None:
        return
    with connections[using].cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_namespace WHERE nspname = %s", [schema])
        if cursor.fetchone() is None:
            cursor.execute(f'CREATE SCHEMA "{schema}"')
