# Test fixtures

Source responses used with respx. They follow the real AniList GraphQL,
Shikimori REST, and Annict GraphQL response shapes, trimmed to the fields the
worker requests. They were written by hand because the sources weren't
reachable when these tests were created; replace them with recordings from the
live APIs when you can (the tests only rely on the ids and fields they assert on).

- `anilist_page.json`: one `Page` with six anime covering the merge cases: full
  data, a sequel relation, a missing English title, an unknown genre, a Japanese-only
  title without `idMal`, an adult title.
- `shikimori_16498.json`, `shikimori_9253.json`: with and without a licensed Russian title;
  BBCode and a spoiler block in the description.
- `annict_works.json`: a `searchWorks` response.
