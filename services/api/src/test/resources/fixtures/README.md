# Test fixtures

`catalog.sql` is loaded after `services/ingest-worker/contract/catalog-schema.sql`. It holds
12 anime with deliberately incomplete translations, to exercise the fallback chains:
- #2 has no Russian title
- #4 has only Japanese and romaji
- #5 is Japanese-only

It also has a sequel pair (#1 ← #2), an adult title (#6), and a `REMOVED` one (#7), plus
genres and tags with missing locales (romance has no Russian name, drama no romaji).
