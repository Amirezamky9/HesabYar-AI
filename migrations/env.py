"""Alembic environment configuration for HesabYar.

Reads HESABYAR_DATABASE_URL from the environment (via get_settings) and falls
back to alembic.ini if the env var is not set.  Targets the HesabYar
declarative Base so autogenerate can detect model changes.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from hesabyar.infrastructure.persistence.postgres.models import Base

# Alembic Config object — provides access to values in alembic.ini.
config = context.config

# Interpret the config file for Python logging (unless we're inside pytest).
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Target metadata for autogenerate support.
target_metadata = Base.metadata


def _get_url() -> str:
    """Resolve the database URL from environment or settings, falling back to alembic.ini."""
    # Direct env var (CI, Docker, or local override) takes priority.
    env_url = os.environ.get("HESABYAR_DATABASE_URL")
    if env_url:
        return env_url

    # Fall back to the application settings module.
    try:
        from hesabyar.infrastructure.config import get_settings

        settings = get_settings()
        return settings.database.sqlalchemy_url
    except Exception:
        # Last resort: use the value from alembic.ini.
        return config.get_main_option("sqlalchemy.url", "")


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode — emit SQL to stdout without a live connection."""
    url = _get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode — connect to the database and apply changes."""
    cfg = config.get_section(config.config_ini_section, {})
    cfg["sqlalchemy.url"] = _get_url()

    connectable = engine_from_config(
        cfg,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
