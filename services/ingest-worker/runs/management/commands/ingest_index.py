from runs.management.commands._base import RunCommand
from runs.models import RunMode


class Command(RunCommand):
    help = "Full Elasticsearch rebuild into anime_v{ES_INDEX_VERSION} with an atomic alias swap"
    mode = RunMode.INDEX
