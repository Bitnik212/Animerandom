from __future__ import annotations

import time
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from pipeline import orchestration
from runs import services as runs
from runs.models import IngestRun, RunStatus


class RunCommand(BaseCommand):
    """Enqueue a run, then wait for it and print stage progress (unless --no-wait)."""

    mode: str = ""

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--no-wait", action="store_true", help="Enqueue and return immediately")

    def params(self, options: dict[str, Any]) -> dict[str, Any]:
        return {}

    def run_mode(self, options: dict[str, Any]) -> str:
        return self.mode

    def handle(self, *args: Any, **options: Any) -> None:
        try:
            run = orchestration.start(
                self.run_mode(options), {**self.params(options), "requested_by": "cli"}
            )
        except runs.RunInProgress as exc:
            raise CommandError(f"run {exc.run_id} is in progress") from exc
        self.stdout.write(f"run {run.id} enqueued ({run.mode})")
        if options["no_wait"]:
            return
        self.wait(run.id)

    def wait(self, run_id: int, poll: float = 5.0) -> None:
        last = ""
        while True:
            run = IngestRun.objects.get(id=run_id)
            line = "  ".join(f"{s.name}:{s.status}" for s in run.stages.all())
            if line != last:
                self.stdout.write(line)
                last = line
            if run.is_finished:
                break
            time.sleep(poll)
        errors = run.item_errors.filter(kind="error").count()
        self.stdout.write(f"run {run.id} {run.status}; item errors: {errors}")
        if run.status != RunStatus.DONE:
            raise CommandError(run.error or f"run {run.id} {run.status}")
