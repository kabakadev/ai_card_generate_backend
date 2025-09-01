import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Load your Flask app and db
from config import app, db

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = db.metadata

# Always use the Flask app's DB URL
with app.app_context():
    db_url = app.config.get("SQLALCHEMY_DATABASE_URI")
    if not db_url:
        raise RuntimeError("SQLALCHEMY_DATABASE_URI is not set")
    config.set_main_option("sqlalchemy.url", db_url)

def _process_revision_directives(context, revision, directives):
    if getattr(config.cmd_opts, "autogenerate", False):
        script = directives[0]
        if script.upgrade_ops.is_empty():
            directives[:] = []
            context.log.info("No changes in schema detected.")

def run_migrations_offline():
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        process_revision_directives=_process_revision_directives,
    )
    with context.begin_transaction():
        context.run_migrations()

def run_migrations_online():
    connectable = engine_from_config(
        config.get_section(config.config_ini_section),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            process_revision_directives=_process_revision_directives,
        )
        with context.begin_transaction():
            context.run_migrations()

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
