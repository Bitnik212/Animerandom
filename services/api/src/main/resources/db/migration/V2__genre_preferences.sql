-- Onboarding genre preferences (POST /users/me/onboarding). Genre slugs, as in catalog.genre.slug;
-- not foreign keys, so a genre renamed or dropped by the ingest worker never blocks it.
ALTER TABLE app.app_user
    ADD COLUMN liked_genres    text[] NOT NULL DEFAULT '{}',
    ADD COLUMN disliked_genres text[] NOT NULL DEFAULT '{}';
