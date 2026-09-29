from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BASE_DIR.parent.parent

env = environ.Env()
environ.Env.read_env(BASE_DIR / ".env")

SECRET_KEY = env("DJANGO_SECRET_KEY")
DEBUG = env.bool("DJANGO_DEBUG", default=False)
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=["ingest-web", "localhost"])

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django_celery_beat",
    "django_celery_results",
    "catalog",
    "runs",
    "pipeline",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",  # admin static files under gunicorn
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "ingest.urls"
ASGI_APPLICATION = "ingest.asgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# Both aliases use the same database and role; only search_path differs, so each
# schema gets its own tables, including its own django_migrations.
_db = env.db_url("POSTGRES_URL")


def _with_search_path(schema: str) -> dict:
    return {**_db, "OPTIONS": {"options": f"-c search_path={schema},public"}}


DATABASES = {
    "default": _with_search_path("ingest"),
    "catalog": _with_search_path("catalog"),
}
DATABASE_ROUTERS = ["ingest.db_router.CatalogRouter"]
DB_SCHEMAS = {"default": "ingest", "catalog": "catalog"}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# --- Celery ---------------------------------------------------------------

CELERY_BROKER_URL = env("CELERY_BROKER_URL", default="redis://redis:6379/1")
CELERY_RESULT_BACKEND = "django-db"
CELERY_RESULT_EXTENDED = True
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TASK_DEFAULT_QUEUE = "pipeline"
CELERY_TIMEZONE = "UTC"
CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"
CELERY_TASK_ALWAYS_EAGER = env.bool("CELERY_TASK_ALWAYS_EAGER", default=False)
CELERY_TASK_EAGER_PROPAGATES = True

# --- Sinks ----------------------------------------------------------------

REDIS_URL = env("REDIS_URL", default="redis://redis:6379/0")
ELASTICSEARCH_URL = env("ELASTICSEARCH_URL", default="http://elasticsearch:9200")
ES_INDEX_VERSION = env.int("ES_INDEX_VERSION", default=1)
ES_ALIAS = "anime"
ES_INDEX_DEFINITION = Path(
    env(
        "ES_INDEX_DEFINITION",
        default=str(REPO_ROOT / "infra" / "elasticsearch" / "anime-index.json"),
    )
)

# --- Sources --------------------------------------------------------------

HTTP_USER_AGENT = env(
    "HTTP_USER_AGENT", default="anime-picker/0.1 (+https://github.com/bitnik212/animerandom)"
)
ANILIST_URL = env("ANILIST_URL", default="https://graphql.anilist.co")
SHIKIMORI_URL = env("SHIKIMORI_URL", default="https://shikimori.io/api")
ANNICT_URL = env("ANNICT_URL", default="https://api.annict.com/graphql")
ANNICT_TOKEN = env("ANNICT_TOKEN", default="")
ARM_URL = env(
    "ARM_URL", default="https://raw.githubusercontent.com/kawaiioverflow/arm/master/arm.json"
)
MANAMI_SNAPSHOT = Path(
    env("MANAMI_SNAPSHOT", default=str(BASE_DIR / "data" / "manami-snapshot.json.gz"))
)

SOURCE_RATES_PER_MIN = {
    "anilist": env.int("RATE_ANILIST_PER_MIN", default=30),
    "shikimori": env.int("RATE_SHIKIMORI_PER_MIN", default=60),
    "annict": env.int("RATE_ANNICT_PER_MIN", default=30),
}
ANILIST_PER_PAGE = env.int("ANILIST_PER_PAGE", default=50)
ANILIST_ID_RANGE = env.int("ANILIST_ID_RANGE", default=5000)
INCREMENTAL_DAYS = env.int("INCREMENTAL_DAYS", default=3)
MERGE_BATCH_SIZE = 200

# Vector matching when no id cross-reference links an anime to Shikimori or Annict
# (pipeline/idmap/vector.py): accept at or above ACCEPT when ahead of the runner-up by
# MARGIN; REVIEW..ACCEPT is reported on the run; a failed search is retried after
# VECTOR_RECHECK_DAYS.
VECTOR_MATCH_ACCEPT = env.float("VECTOR_MATCH_ACCEPT", default=0.80)
VECTOR_MATCH_MARGIN = env.float("VECTOR_MATCH_MARGIN", default=0.08)
VECTOR_MATCH_REVIEW = env.float("VECTOR_MATCH_REVIEW", default=0.60)
VECTOR_RECHECK_DAYS = env.int("VECTOR_RECHECK_DAYS", default=30)

# --- Auth for the ninja API -----------------------------------------------

KEYCLOAK_INTERNAL_URL = env("KEYCLOAK_INTERNAL_URL", default="http://keycloak:8080")
KEYCLOAK_ISSUER = env("KEYCLOAK_ISSUER", default="http://localhost:8180/realms/anime-picker")
KEYCLOAK_REALM = env("KEYCLOAK_REALM", default="anime-picker")
KEYCLOAK_CLIENT_ID = env("KEYCLOAK_CLIENT_ID", default="anime-picker-api")
JWT_LEEWAY_SECONDS = env.int("JWT_LEEWAY_SECONDS", default=30)
FLOWER_URL = env("FLOWER_URL", default="http://localhost:5555")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": env("LOG_LEVEL", default="INFO")},
}
