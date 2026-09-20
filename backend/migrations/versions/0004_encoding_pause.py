"""Persist requested and acknowledged encoding pause state."""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("tasks", sa.Column("can_pause", sa.Boolean(), server_default=sa.false(), nullable=False))
    op.add_column(
        "tasks", sa.Column("pause_requested", sa.Boolean(), server_default=sa.false(), nullable=False)
    )
    op.add_column("tasks", sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True))


def downgrade():
    op.drop_column("tasks", "paused_at")
    op.drop_column("tasks", "pause_requested")
    op.drop_column("tasks", "can_pause")
