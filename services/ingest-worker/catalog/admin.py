"""Catalog admin: read-only except overrides. Saving an override enqueues a refresh."""

from __future__ import annotations

import json
from typing import Any

from django.contrib import admin, messages
from django.http import HttpRequest
from django.utils.html import format_html, format_html_join

from catalog.models import Anime, AnimeLocalization, AnimeOverride, Genre, RawSourceRecord, Tag
from pipeline.localize.fallback import display_title
from pipeline.models import IdMapping


def enqueue_refresh(request: HttpRequest, anilist_id: int) -> None:
    from pipeline import orchestration
    from runs.models import RunMode
    from runs.services import RunInProgress

    try:
        run = orchestration.start(
            RunMode.REFRESH,
            {"anilist_ids": [anilist_id], "requested_by": request.user.get_username()},
        )
        messages.info(request, f"Refresh of AniList {anilist_id} enqueued as run {run.id}.")
    except RunInProgress as exc:
        messages.warning(
            request,
            f"Run {exc.run_id} is in progress; the override applies on the next run "
            f"(or refresh AniList {anilist_id} once it finishes).",
        )


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def get_readonly_fields(self, request: HttpRequest, obj: Any = None) -> list[str]:
        return [f.name for f in self.model._meta.fields] + list(self.readonly_fields)


class OverrideInline(admin.TabularInline):
    model = AnimeOverride
    extra = 0
    fields = ("field", "locale", "value", "note", "updated_by", "updated_at")
    readonly_fields = ("updated_by", "updated_at")


@admin.register(Anime)
class AnimeAdmin(ReadOnlyAdmin):
    list_display = (
        "id",
        "title",
        "anilist_id",
        "format",
        "status",
        "season_year",
        "score",
        "popularity",
    )
    list_filter = ("status", "format", "is_adult")
    search_fields = ("=anilist_id", "=mal_id", "localizations__title", "synonyms__value")
    readonly_fields = ("localization_table", "raw_payloads")
    inlines = [OverrideInline]
    ordering = ("-popularity",)

    def get_queryset(self, request: HttpRequest) -> Any:
        return super().get_queryset(request).prefetch_related("localizations").distinct()

    @admin.display(description="Title")
    def title(self, obj: Anime) -> str:
        titles = {loc.locale: loc.title for loc in obj.localizations.all()}
        return display_title(titles) or "—"

    @admin.display(description="Localizations")
    def localization_table(self, obj: Anime) -> str:
        rows = AnimeLocalization.objects.filter(anime=obj).order_by("locale")
        body = format_html_join(
            "",
            "<tr><td>{}</td><td>{}<br><small>{}{}</small></td>"
            "<td style='white-space:pre-wrap'>{}<br><small>{}{}</small></td></tr>",
            (
                (
                    r.locale,
                    r.title or "—",
                    r.title_source or "",
                    " · machine" if r.title_machine else "",
                    (r.synopsis or "—")[:600],
                    r.synopsis_source or "",
                    " · machine" if r.synopsis_machine else "",
                )
                for r in rows
            ),
        )
        return format_html(
            "<table><tr><th>Locale</th><th>Title</th><th>Synopsis</th></tr>{}</table>", body
        )

    @admin.display(description="Raw payloads")
    def raw_payloads(self, obj: Anime) -> str:
        mapping = IdMapping.objects.filter(anilist_id=obj.anilist_id).first()
        keys = [("anilist", str(obj.anilist_id))]
        if mapping and mapping.shikimori_id:
            keys.append(("shikimori", mapping.shikimori_id))
        if mapping and mapping.annict_id:
            keys.append(("annict", str(mapping.annict_id)))
        cells = []
        for source, source_id in keys:
            rec = RawSourceRecord.objects.filter(source=source, source_id=source_id).first()
            text = json.dumps(rec.payload, ensure_ascii=False, indent=1) if rec else "not fetched"
            fetched = f"fetched {rec.fetched_at:%Y-%m-%d %H:%M} UTC" if rec else ""
            cells.append((source, fetched, text))
        return format_html(
            "<div style='display:flex;gap:1em'>{}</div>",
            format_html_join(
                "",
                "<div style='flex:1;min-width:0'><b>{}</b> <small>{}</small>"
                "<pre style='max-height:30em;overflow:auto;white-space:pre-wrap'>{}</pre></div>",
                cells,
            ),
        )

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return True  # every field is read-only; only the override inline can change

    def save_formset(self, request: HttpRequest, form: Any, formset: Any, change: bool) -> None:
        instances = formset.save(commit=False)
        for instance in instances:
            instance.updated_by = request.user.get_username()
            instance.save()
        for obj in formset.deleted_objects:
            obj.delete()
        if instances or formset.deleted_objects:
            enqueue_refresh(request, form.instance.anilist_id)


@admin.register(AnimeOverride)
class AnimeOverrideAdmin(admin.ModelAdmin):
    list_display = ("anime", "field", "locale", "value", "updated_by", "updated_at")
    list_filter = ("field", "locale")
    search_fields = ("=anime__anilist_id", "value", "note")
    raw_id_fields = ("anime",)
    readonly_fields = ("updated_by", "updated_at")

    def save_model(self, request: HttpRequest, obj: AnimeOverride, form: Any, change: bool) -> None:
        obj.updated_by = request.user.get_username()
        super().save_model(request, obj, form, change)
        enqueue_refresh(request, obj.anime.anilist_id)

    def delete_model(self, request: HttpRequest, obj: AnimeOverride) -> None:
        anilist_id = obj.anime.anilist_id
        super().delete_model(request, obj)
        enqueue_refresh(request, anilist_id)


class VocabAdmin(ReadOnlyAdmin):
    list_display: tuple[str, ...] = ("slug", "names")
    search_fields = ("slug", "localizations__name")

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    @admin.display(description="Names")
    def names(self, obj: Any) -> str:
        return " · ".join(f"{loc.locale}: {loc.name}" for loc in obj.localizations.all())


@admin.register(Genre)
class GenreAdmin(VocabAdmin):
    pass


@admin.register(Tag)
class TagAdmin(VocabAdmin):
    list_display = ("slug", "category", "is_spoiler", "names")
    list_filter = ("is_spoiler", "category")
