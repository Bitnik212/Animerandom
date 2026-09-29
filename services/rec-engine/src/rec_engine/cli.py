"""`rec` command: migrate, embed, train, evaluate."""

from __future__ import annotations

import json
import logging

import typer

from rec_engine import jobs
from rec_engine.db import Database

app = typer.Typer(help="Rec engine jobs", no_args_is_help=True)


@app.callback()
def main(verbose: bool = False) -> None:
    logging.basicConfig(level=logging.INFO if verbose else logging.WARNING)


def _print(result: object) -> None:
    typer.echo(json.dumps(result, indent=2, default=str))


@app.command()
def migrate() -> None:
    """Apply Alembic migrations to schema `rec` (the server also does this on start)."""
    jobs.migrate()
    typer.echo("rec schema is at head")


@app.command()
def embed(
    force: bool = typer.Option(False, help="Re-embed everything, not only changed rows"),
) -> None:
    """Embed anime whose text is new or changed."""
    _print(jobs.embed_job(Database(), force=force))


@app.command()
def train() -> None:
    """Train ALS on all interactions and activate the new model."""
    _print(jobs.train_job(Database()))


@app.command()
def evaluate(holdout: float = 0.2, k: int = 20, seed: int = 42) -> None:
    """Offline recall@k and nDCG@k on a held-out share of ratings ≥ 8."""
    _print(jobs.evaluate_job(Database(), holdout=holdout, k=k, seed=seed))


if __name__ == "__main__":
    app()
