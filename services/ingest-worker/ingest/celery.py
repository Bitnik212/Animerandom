import os

from celery import Celery
from kombu import Queue

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "ingest.settings")

SOURCES = ("anilist", "shikimori", "annict")
QUEUES = ("pipeline", *(f"source.{s}" for s in SOURCES), "index")

app = Celery("ingest")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.conf.task_queues = [Queue(q) for q in QUEUES]
app.conf.task_routes = {
    "pipeline.tasks.fetch_anilist_*": {"queue": "source.anilist"},
    "pipeline.tasks.fetch_shikimori": {"queue": "source.shikimori"},
    "pipeline.tasks.fetch_annict": {"queue": "source.annict"},
    "pipeline.tasks.index_elasticsearch": {"queue": "index"},
    "pipeline.tasks.rebuild_pools": {"queue": "index"},
    "pipeline.tasks.*": {"queue": "pipeline"},
}
app.autodiscover_tasks(["pipeline"])
