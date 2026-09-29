-- Schema `app`, owned by the API (role api_svc). Tables per infra/README.md.
-- Never edit an applied migration; add V2__... instead.

CREATE TABLE app.app_user (
    id                      uuid        PRIMARY KEY,          -- Keycloak `sub`
    display_name            text,
    locale                  text        NOT NULL DEFAULT 'en'
                            CHECK (locale IN ('en', 'ru', 'ja', 'ja_latn')),
    show_adult              boolean     NOT NULL DEFAULT false,
    onboarding_completed_at timestamptz,
    last_seen_at            timestamptz,
    created_at              timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE app.user_anime (
    user_id    uuid        NOT NULL REFERENCES app.app_user (id) ON DELETE CASCADE,
    anime_id   bigint      NOT NULL REFERENCES catalog.anime (id),
    status     text        NOT NULL CHECK (status IN ('planned', 'watching', 'completed', 'dropped')),
    score      smallint    CHECK (score BETWEEN 1 AND 10),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, anime_id)
);
CREATE INDEX user_anime_user_status_idx ON app.user_anime (user_id, status);

CREATE TABLE app.user_feedback (
    id         bigserial   PRIMARY KEY,
    user_id    uuid        NOT NULL REFERENCES app.app_user (id) ON DELETE CASCADE,
    anime_id   bigint      NOT NULL REFERENCES catalog.anime (id),
    kind       text        NOT NULL CHECK (kind IN ('not_interested', 'skipped')),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX user_feedback_user_kind_idx ON app.user_feedback (user_id, kind);
