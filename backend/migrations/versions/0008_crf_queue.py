"""Add an independent CRF analysis limit without changing existing settings."""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("queue_settings") as batch:
        batch.add_column(sa.Column("max_crf_tasks", sa.Integer(), nullable=False, server_default="1"))
        batch.create_check_constraint("queue_crf_limit_bounds", "max_crf_tasks BETWEEN 1 AND 64")


def downgrade():
    with op.batch_alter_table("queue_settings") as batch:
        batch.drop_constraint("queue_crf_limit_bounds", type_="check")
        batch.drop_column("max_crf_tasks")
