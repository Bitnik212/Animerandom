from typing import Any

from django.core.management.base import BaseCommand

from pipeline import vocab


class Command(BaseCommand):
    help = "Sync vocab/genres.yaml and vocab/tags.yaml into Postgres"

    def handle(self, *args: Any, **options: Any) -> None:
        counts = vocab.sync()
        self.stdout.write(f"synced {counts['genres']} genres, {counts['tags']} tags")
