from django.apps import AppConfig


class CatalogConfig(AppConfig):
    name = "catalog"

    def ready(self) -> None:
        # Registers the pre_migrate handler that creates missing schemas.
        from ingest import db_router  # noqa: F401
