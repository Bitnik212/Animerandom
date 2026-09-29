from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandParser

from runs.contract import CONTRACT_PATH, dump_catalog_schema


class Command(BaseCommand):
    help = "Write contract/catalog-schema.sql: a schema-only dump of the migrated catalog schema"

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--output", default=str(CONTRACT_PATH))

    def handle(self, *args: Any, **options: Any) -> None:
        sql = dump_catalog_schema(settings.DATABASES["catalog"])
        Path(options["output"]).write_text(sql, encoding="utf-8")
        self.stdout.write(f"wrote {options['output']}")
