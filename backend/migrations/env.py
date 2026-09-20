from alembic import context

import shared.models  # noqa: F401
from shared.db import Base, engine

with engine().connect() as connection:
    context.configure(connection=connection, target_metadata=Base.metadata)
    with context.begin_transaction():
        context.run_migrations()
