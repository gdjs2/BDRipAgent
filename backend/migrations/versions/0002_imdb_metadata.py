"""Preserve the IMDb ID and metadata used to name each release."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("movie_jobs", sa.Column("imdb_id", sa.String(16), nullable=True))
    op.add_column(
        "movie_jobs",
        sa.Column("imdb_metadata", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=True),
    )


def downgrade():
    op.drop_column("movie_jobs", "imdb_metadata")
    op.drop_column("movie_jobs", "imdb_id")
