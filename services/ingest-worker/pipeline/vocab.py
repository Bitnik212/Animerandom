"""Genre and tag vocabulary: YAML in git is the source of truth, Postgres is a copy."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

import yaml
from django.conf import settings
from django.db import transaction

from catalog.models import LOCALES, Genre, GenreLocalization, Tag, TagLocalization

VOCAB_DIR = settings.BASE_DIR / "vocab"


@dataclass(frozen=True)
class Term:
    slug: str
    sources: dict[str, str]
    names: dict[str, str]
    spoiler: bool = False


@dataclass(frozen=True)
class Vocabulary:
    terms: tuple[Term, ...]
    _index: dict[tuple[str, str], str] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        index = {
            (source, source_name.casefold()): term.slug
            for term in self.terms
            for source, source_name in term.sources.items()
        }
        object.__setattr__(self, "_index", index)

    def slug_for(self, source: str, name: str) -> str | None:
        return self._index.get((source, name.casefold()))


def _load(path: Path) -> Vocabulary:
    with path.open(encoding="utf-8") as f:
        rows = yaml.safe_load(f) or []
    terms = []
    for row in rows:
        names = {k: v for k, v in (row.get("names") or {}).items() if k in LOCALES and v}
        terms.append(
            Term(
                slug=row["slug"],
                sources=row.get("sources") or {},
                names=names,
                spoiler=bool(row.get("spoiler", False)),
            )
        )
    slugs = [t.slug for t in terms]
    if len(slugs) != len(set(slugs)):
        raise ValueError(f"{path.name}: duplicate slugs")
    return Vocabulary(tuple(terms))


@cache
def genres() -> Vocabulary:
    return _load(VOCAB_DIR / "genres.yaml")


@cache
def tags() -> Vocabulary:
    return _load(VOCAB_DIR / "tags.yaml")


@transaction.atomic(using="catalog")
def sync() -> dict[str, int]:
    """Upsert YAML terms and their names into Postgres. Terms removed from YAML stay
    in Postgres (anime may still reference them) but lose nothing else."""
    targets: tuple[tuple[Vocabulary, Any, Any, str], ...] = (
        (genres(), Genre, GenreLocalization, "genre"),
        (tags(), Tag, TagLocalization, "tag"),
    )
    for vocab, model, loc_model, fk in targets:
        for term in vocab.terms:
            # Spoiler flags only ever get set here: AniList's isGeneralSpoiler also
            # sets them at write time.
            defaults = {"is_spoiler": True} if model is Tag and term.spoiler else {}
            obj, _ = model.objects.update_or_create(slug=term.slug, defaults=defaults)
            loc_model.objects.filter(**{fk: obj}).exclude(locale__in=list(term.names)).delete()
            loc_model.objects.bulk_create(
                [
                    loc_model(**{fk: obj}, locale=locale, name=name)
                    for locale, name in term.names.items()
                ],
                update_conflicts=True,
                unique_fields=[f"{fk}_id", "locale"],
                update_fields=["name"],
            )
    return {"genres": len(genres().terms), "tags": len(tags().terms)}
