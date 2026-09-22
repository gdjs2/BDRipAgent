"""Replace the combined job limit with encoding and other-task limits."""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    # The old limit counted jobs, so it cannot be carried over as either task limit.
    # Keep pause state and all queued/running tasks, starting the new pools at 1/3.
    with op.batch_alter_table("queue_settings") as batch:
        batch.drop_constraint("queue_limit_bounds", type_="check")
        batch.drop_column("max_concurrent_jobs")
        batch.add_column(sa.Column("max_encoding_tasks", sa.Integer(), nullable=False, server_default="1"))
        batch.add_column(sa.Column("max_other_tasks", sa.Integer(), nullable=False, server_default="3"))
        batch.create_check_constraint("queue_encoding_limit_bounds", "max_encoding_tasks BETWEEN 1 AND 64")
        batch.create_check_constraint("queue_other_limit_bounds", "max_other_tasks BETWEEN 1 AND 64")


def downgrade():
    with op.batch_alter_table("queue_settings") as batch:
        batch.add_column(sa.Column("max_concurrent_jobs", sa.Integer(), nullable=False, server_default="1"))
    op.execute("UPDATE queue_settings SET max_concurrent_jobs = max_encoding_tasks")
    with op.batch_alter_table("queue_settings") as batch:
        batch.drop_constraint("queue_encoding_limit_bounds", type_="check")
        batch.drop_constraint("queue_other_limit_bounds", type_="check")
        batch.drop_column("max_encoding_tasks")
        batch.drop_column("max_other_tasks")
        batch.alter_column("max_concurrent_jobs", server_default=None)
        batch.create_check_constraint("queue_limit_bounds", "max_concurrent_jobs BETWEEN 1 AND 64")
