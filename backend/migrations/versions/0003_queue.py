"""Persistent queue controls and ordering, preserving existing tasks."""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("tasks", sa.Column("held", sa.Boolean(), server_default=sa.false(), nullable=False))
    op.add_column("tasks", sa.Column("queue_priority", sa.Integer(), server_default="0", nullable=False))
    table = op.create_table(
        "queue_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("max_concurrent_jobs", sa.Integer(), nullable=False),
        sa.Column("paused", sa.Boolean(), nullable=False),
        sa.CheckConstraint("id = 1", name="queue_singleton"),
        sa.CheckConstraint("max_concurrent_jobs BETWEEN 1 AND 64", name="queue_limit_bounds"),
    )
    op.bulk_insert(table, [{"id": 1, "max_concurrent_jobs": 1, "paused": False}])


def downgrade():
    op.drop_table("queue_settings")
    op.drop_column("tasks", "queue_priority")
    op.drop_column("tasks", "held")
