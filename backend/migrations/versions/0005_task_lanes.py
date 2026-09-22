"""Allow background track review alongside the video pipeline."""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("tasks", sa.Column("lane", sa.String(20), nullable=False, server_default="pipeline"))
    op.drop_index("one_active_task_per_job", table_name="tasks")
    op.create_index(
        "one_active_task_per_lane",
        "tasks",
        ["job_id", "lane"],
        unique=True,
        postgresql_where=sa.text("status IN ('QUEUED','RUNNING')"),
        sqlite_where=sa.text("status IN ('QUEUED','RUNNING')"),
    )


def downgrade():
    # A downgrade needs a drained queue: duplicate active lanes cannot fit the old invariant.
    op.drop_index("one_active_task_per_lane", table_name="tasks")
    op.create_index(
        "one_active_task_per_job",
        "tasks",
        ["job_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('QUEUED','RUNNING')"),
        sqlite_where=sa.text("status IN ('QUEUED','RUNNING')"),
    )
    op.drop_column("tasks", "lane")
