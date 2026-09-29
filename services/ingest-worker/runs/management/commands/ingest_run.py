from typing import Any

from django.core.management.base import CommandError, CommandParser

from runs.management.commands._base import RunCommand
from runs.models import RunMode

SOURCES = ("anilist", "shikimori", "annict")


class Command(RunCommand):
    help = "Start a full or incremental ingest run"

    def add_arguments(self, parser: CommandParser) -> None:
        super().add_arguments(parser)
        mode = parser.add_mutually_exclusive_group(required=True)
        mode.add_argument("--full", action="store_true")
        mode.add_argument("--incremental", action="store_true")
        parser.add_argument(
            "--limit", type=int, help="Top N anime by AniList popularity (full runs)"
        )
        parser.add_argument("--sources", help=f"Comma-separated subset of {','.join(SOURCES)}")
        parser.add_argument(
            "--force", action="store_true", help="Re-merge even when inputs are unchanged"
        )

    def run_mode(self, options: dict[str, Any]) -> str:
        return RunMode.FULL if options["full"] else RunMode.INCREMENTAL

    def params(self, options: dict[str, Any]) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if options["limit"]:
            params["limit"] = options["limit"]
        if options["sources"]:
            sources = [s.strip() for s in options["sources"].split(",") if s.strip()]
            unknown = set(sources) - set(SOURCES)
            if unknown:
                raise CommandError(f"unknown sources: {', '.join(sorted(unknown))}")
            params["sources"] = sources
        if options["force"]:
            params["force"] = True
        return params
