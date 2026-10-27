"""Alembic environment: online mode only, on a connection supplied by `ai_oil.db.migrate.upgrade`."""

from alembic import context

config = context.config

if context.is_offline_mode():
    raise NotImplementedError("offline migrations are not supported")

connection = config.attributes["connection"]
context.configure(connection=connection, target_metadata=None, render_as_batch=True)
with context.begin_transaction():
    context.run_migrations()
