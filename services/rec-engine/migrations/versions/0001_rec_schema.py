"""anime_embedding and rec_model

Revision ID: 0001
Revises:
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # vector(384) matches paraphrase-multilingual-MiniLM-L12-v2. A model with another
    # dimension needs a new migration and a full re-embed.
    op.execute(
        """
        CREATE TABLE rec.anime_embedding (
            anime_id    bigint      NOT NULL REFERENCES catalog.anime (id),
            model       text        NOT NULL,
            embedding   vector(384) NOT NULL,
            source_hash text        NOT NULL,
            updated_at  timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (anime_id, model)
        )
        """
    )
    op.execute(
        "CREATE INDEX anime_embedding_hnsw ON rec.anime_embedding "
        "USING hnsw (embedding vector_cosine_ops)"
    )
    op.execute(
        """
        CREATE TABLE rec.rec_model (
            id            bigserial   PRIMARY KEY,
            kind          text        NOT NULL,          -- als
            artifact_path text        NOT NULL,
            trained_at    timestamptz NOT NULL DEFAULT now(),
            metrics       jsonb       NOT NULL DEFAULT '{}'::jsonb,
            is_active     boolean     NOT NULL DEFAULT false
        )
        """
    )
    op.execute("CREATE UNIQUE INDEX rec_model_one_active ON rec.rec_model (kind) WHERE is_active")


def downgrade() -> None:
    op.execute("DROP TABLE rec.rec_model")
    op.execute("DROP TABLE rec.anime_embedding")
