from typing import Any

from django.core.management.base import CommandParser

from runs.management.commands._base import RunCommand
from runs.models import RunMode


class Command(RunCommand):
    help = "Refetch specific anime from every source, then merge, write, and index them"
    mode = RunMode.REFRESH

    def add_arguments(self, parser: CommandParser) -> None:
        super().add_arguments(parser)
        parser.add_argument("source", choices=["anilist"], help="Id space of --ids")
        parser.add_argument("--ids", required=True, help="Comma-separated AniList ids")

    def params(self, options: dict[str, Any]) -> dict[str, Any]:
        return {"anilist_ids": [int(i) for i in options["ids"].split(",") if i.strip()]}
