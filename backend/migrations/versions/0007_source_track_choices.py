"""Store confirmed track choices once per immutable source."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "source_track_choices",
        sa.Column("source_key", sa.String(64), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("data", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("source_track_choices")
