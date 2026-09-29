from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from pipeline.idmap import arm, manami


class Command(BaseCommand):
    help = "Load id cross-references: download arm, and/or load the manami snapshot"

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--arm", action="store_true", help="Download and load arm.json")
        parser.add_argument(
            "--manami", nargs="?", const="", help="Load the snapshot (default path from settings)"
        )

    def handle(self, *args: Any, **options: Any) -> None:
        if options["arm"]:
            self.stdout.write(f"arm: {arm.refresh()} entries")
        if options["manami"] is not None:
            path = Path(options["manami"]) if options["manami"] else None
            self.stdout.write(f"manami: {manami.load(path)} entries")
