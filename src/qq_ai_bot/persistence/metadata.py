"""Aggregate every SQLAlchemy model into the deployment metadata.

Domain-owned tables live beside their repositories.  Importing this module is
the single supported way for Alembic and test schema creation to discover all
of them without turning :mod:`qq_ai_bot.persistence.models` into a monolith.
"""

from qq_ai_bot.persistence.models import Base

# These imports are intentionally side-effectful: defining each mapped class
# registers its table on ``Base.metadata``.
from qq_ai_bot.planner import db_models as _planner_db_models  # noqa: F401
from qq_ai_bot.plugin_host import db_models as _plugin_db_models  # noqa: F401

__all__ = ["Base"]
