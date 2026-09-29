from __future__ import annotations

from typing import Any

from django.conf import settings
from django.contrib import admin
from django.http import HttpRequest
from django.utils.html import format_html

from runs.models import IngestRun, RunItemError, RunStage


class StageInline(admin.TabularInline):
    model = RunStage
    extra = 0
    can_delete = False
    fields = ("name", "status", "counts", "started_at", "finished_at")
    readonly_fields = fields

    def has_add_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


class ItemErrorInline(admin.TabularInline):
    model = RunItemError
    extra = 0
    can_delete = False
    fields = ("stage", "kind", "source", "item_id", "message", "created_at")
    readonly_fields = fields
    max_num = 0

    def get_queryset(self, request: HttpRequest) -> Any:
        return super().get_queryset(request).order_by("-id")


@admin.register(IngestRun)
class IngestRunAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "mode",
        "status",
        "created_at",
        "started_at",
        "finished_at",
        "error_count",
    )
    list_filter = ("mode", "status")
    readonly_fields = (
        "mode",
        "status",
        "params",
        "cancel_requested",
        "error",
        "created_at",
        "started_at",
        "finished_at",
        "flower",
    )
    inlines = [StageInline, ItemErrorInline]

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    @admin.display(description="Item errors")
    def error_count(self, obj: IngestRun) -> int:
        return obj.item_errors.filter(kind="error").count()

    @admin.display(description="Flower")
    def flower(self, obj: IngestRun) -> str:
        return format_html(
            '<a href="{}/tasks" target="_blank">Task monitor</a>', settings.FLOWER_URL.rstrip("/")
        )


@admin.register(RunItemError)
class RunItemErrorAdmin(admin.ModelAdmin):
    list_display = ("run", "stage", "kind", "source", "item_id", "message", "created_at")
    list_filter = ("stage", "kind", "source")
    search_fields = ("item_id", "message")

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False
