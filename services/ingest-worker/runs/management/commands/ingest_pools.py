from runs.management.commands._base import RunCommand
from runs.models import RunMode


class Command(RunCommand):
    help = "Rebuild the Redis pool:* sets"
    mode = RunMode.POOLS
