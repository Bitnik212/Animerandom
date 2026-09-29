from typing import Any

from django.core.management.base import CommandParser

from runs.management.commands._base import RunCommand
from runs.models import RunMode


class Command(RunCommand):
    help = "Re-merge every anime from raw_source_record (no network), then index and pool"
    mode = RunMode.MERGE

    def add_arguments(self, parser: CommandParser) -> None:
        super().add_arguments(parser)
        parser.add_argument(
            "--force", action="store_true", help="Rewrite rows even if inputs are unchanged"
        )

    def params(self, options: dict[str, Any]) -> dict[str, Any]:
        return {"force": True} if options["force"] else {}
