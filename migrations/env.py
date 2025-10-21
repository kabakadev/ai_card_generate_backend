# migrations/env.py
import logging
from logging.config import fileConfig
from flask import current_app
from alembic import context

config = context.config
fileConfig(config.config_file_name)
logger = logging.getLogger("alembic.env")

def get_engine():
    try:
        return current_app.extensions["migrate"].db.get_engine()
    except (TypeError, AttributeError):
        return current_app.extensions["migrate"].db.engine

def get_engine_url():
    try:
        return get_engine().url.render_as_string(hide_password=False).replace("%", "%%")
    except AttributeError:
        return str(get_engine().url).replace("%", "%%")

# Make sure Alembic knows the DB URL
config.set_main_option("sqlalchemy.url", get_engine_url())

# --- CRITICAL: import models so metadata is fully populated
import models                      # registers all models (incl. TeacherInvite)
from models import db as models_db # use the *same* metadata object
target_metadata = models_db.metadata

def include_object(obj, name, type_, reflected, compare_to):
    """
    Only manage our own public tables / indexes.
    Never touch Alembic's bookkeeping table.
    """
    # never autogenerate changes for the version table
    if type_ == "table" and name == "alembic_version":
        return False

    schema = getattr(obj, "schema", None)
    if schema not in (None, "public"):
        return False

    our_tables = set(target_metadata.tables.keys())

    if type_ == "table":
        return name in our_tables
    if type_ == "index":
        try:
            return obj.table is not None and obj.table.name in our_tables
        except Exception:
            return False

    # columns/constraints: include if parent table is ours
    parent = getattr(getattr(obj, "table", None), "name", None)
    return (parent in our_tables) if parent else True

def process_revision_directives(ctx, revision, directives):
    # avoid creating empty migration files
    if getattr(config.cmd_opts, "autogenerate", False):
        script = directives[0]
        if script.upgrade_ops.is_empty():
            directives[:] = []
            logger.info("No changes in schema detected.")

def run_migrations_offline():
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        compare_server_default=True,
        version_table_schema="public",
        include_object=include_object,
        process_revision_directives=process_revision_directives,
    )
    with context.begin_transaction():
        context.run_migrations()

def run_migrations_online():
    connectable = get_engine()
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            include_object=include_object,
            process_revision_directives=process_revision_directives,
            version_table_schema="public",
        )
        with context.begin_transaction():
            context.run_migrations()

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
