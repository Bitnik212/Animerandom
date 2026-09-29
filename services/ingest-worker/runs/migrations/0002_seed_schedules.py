"""Seed the periodic tasks. They stay editable in the admin under Periodic tasks."""

import json

from django.db import migrations

SCHEDULES = [
    # name, task, cron (minute, hour, day_of_week), kwargs
    ("Incremental run (daily)", "pipeline.tasks.scheduled_run", ("0", "2", "*"), {"mode": "incremental"}),
    ("Full run with index rebuild (weekly)", "pipeline.tasks.scheduled_run", ("0", "3", "0"), {"mode": "full"}),
    ("Refresh arm id map (weekly)", "pipeline.tasks.refresh_arm", ("30", "1", "0"), {}),
]


def seed(apps, schema_editor):
    CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    for name, task, (minute, hour, day_of_week), kwargs in SCHEDULES:
        crontab, _ = CrontabSchedule.objects.get_or_create(
            minute=minute, hour=hour, day_of_week=day_of_week, day_of_month="*",
            month_of_year="*", timezone="UTC",
        )
        PeriodicTask.objects.get_or_create(
            name=name,
            defaults={"task": task, "crontab": crontab, "kwargs": json.dumps(kwargs), "queue": "pipeline"},
        )


def unseed(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name__in=[s[0] for s in SCHEDULES]).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("runs", "0001_initial"),
        ("django_celery_beat", "0019_alter_periodictasks_options"),
    ]

    operations = [migrations.RunPython(seed, unseed)]
