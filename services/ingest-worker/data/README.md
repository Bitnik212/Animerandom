# data

`manami-snapshot.json.gz` holds AniList → MAL id pairs extracted from the last
release of [manami-project/anime-offline-database](https://github.com/manami-project/anime-offline-database)
(release 2026-07-04, the project was archived afterwards). Only entries with
exactly one AniList and one MAL source are kept. The original data is licensed
under ODbL 1.0 + DbCL 1.0; this extract is under the same license.

Loaded into `ingest.pipeline_idsourceentry` by `manage.py ingest_idmap --manami`.
