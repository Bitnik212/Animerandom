-- Stand-in for schema `app` (owned by the API, migrated by Flyway), written from the
-- table spec in infra/README.md. Replace with the API's migrations once they exist.
CREATE SCHEMA app;

CREATE TABLE app.app_user (
    id                      uuid PRIMARY KEY,
    display_name            text,
    locale                  text,
    show_adult              boolean NOT NULL DEFAULT false,
    onboarding_completed_at timestamptz,
    last_seen_at            timestamptz,
    created_at              timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE app.user_anime (
    user_id    uuid     NOT NULL REFERENCES app.app_user (id),
    anime_id   bigint   NOT NULL REFERENCES catalog.anime (id),
    status     text     NOT NULL CHECK (status IN ('planned', 'watching', 'completed', 'dropped')),
    score      smallint CHECK (score BETWEEN 1 AND 10),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, anime_id)
);

CREATE TABLE app.user_feedback (
    id         bigserial PRIMARY KEY,
    user_id    uuid   NOT NULL REFERENCES app.app_user (id),
    anime_id   bigint NOT NULL REFERENCES catalog.anime (id),
    kind       text   NOT NULL CHECK (kind IN ('not_interested', 'skipped')),
    created_at timestamptz NOT NULL DEFAULT now()
);
