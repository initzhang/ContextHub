"""Database backend compatibility layer.

Abstracts SQL dialect differences between PostgreSQL and openGauss,
allowing the rest of the application to remain backend-agnostic.

Key differences handled here:
- PostgreSQL uses **pgvector** extension (``CREATE EXTENSION vector``).
  openGauss 6.x uses its own **DataVec** extension (``CREATE EXTENSION datavec``);
  openGauss 7.0+ ships DataVec as a built-in kernel feature, so no
  ``CREATE EXTENSION`` is needed at all for vector types.
- PostgreSQL uses ``pgcrypto`` for ``gen_random_uuid()``.
  openGauss uses ``uuid-ossp``.
"""

from __future__ import annotations

from enum import Enum


class DatabaseBackend(str, Enum):
    POSTGRES = "postgres"
    OPENGAUSS = "opengauss"


class DatabaseDialect:
    """Encapsulates SQL dialect differences between backends."""

    def __init__(self, backend: DatabaseBackend):
        self.backend = backend

    @property
    def is_postgres(self) -> bool:
        return self.backend == DatabaseBackend.POSTGRES

    @property
    def is_opengauss(self) -> bool:
        return self.backend == DatabaseBackend.OPENGAUSS

    # ------------------------------------------------------------------
    # Extension management
    # ------------------------------------------------------------------

    def create_vector_extension_sql(self) -> str | None:
        """SQL to enable vector types.

        - PostgreSQL: ``CREATE EXTENSION IF NOT EXISTS vector`` (pgvector).
        - openGauss 6.x: ``CREATE EXTENSION IF NOT EXISTS datavec``.
        - openGauss 7.0+: vector types are built into the kernel — returns
          the ``datavec`` command which is a harmless no-op if the type
          is already present, and ensures 6.x compatibility.

        Returns *None* only when absolutely no command is needed (reserved
        for future use); callers should skip execution when *None*.
        """
        if self.is_postgres:
            return "CREATE EXTENSION IF NOT EXISTS vector"
        # openGauss: DataVec extension (6.x needs it; 7.0+ tolerates it)
        return "CREATE EXTENSION IF NOT EXISTS datavec"

    def create_uuid_extension_sql(self) -> str:
        if self.is_postgres:
            return "CREATE EXTENSION IF NOT EXISTS pgcrypto"
        return "CREATE EXTENSION IF NOT EXISTS uuid-ossp"

    # ------------------------------------------------------------------
    # UUID generation
    # ------------------------------------------------------------------

    def uuid_generate_default(self) -> str:
        """SQL DEFAULT expression for a UUID primary key."""
        if self.is_postgres:
            return "gen_random_uuid()"
        return "gen_random_uuid()"

    # ------------------------------------------------------------------
    # Vector index
    # ------------------------------------------------------------------

    def hnsw_index_sql(
        self,
        index_name: str,
        table: str,
        column: str,
        ops_class: str = "vector_cosine_ops",
        m: int = 16,
        ef_construction: int = 64,
    ) -> str:
        """CREATE INDEX statement for HNSW vector index."""
        return (
            f"CREATE INDEX {index_name} ON {table} "
            f"USING hnsw ({column} {ops_class}) "
            f"WITH (m = {m}, ef_construction = {ef_construction})"
        )

    # ------------------------------------------------------------------
    # Trigger / notify
    # ------------------------------------------------------------------

    def notify_trigger_function_sql(self) -> str:
        """CREATE FUNCTION for the change_events NOTIFY trigger."""
        return (
            "CREATE OR REPLACE FUNCTION notify_change_event() RETURNS trigger AS $$\n"
            "BEGIN\n"
            "    PERFORM pg_notify('context_changed', NEW.context_id::text);\n"
            "    RETURN NEW;\n"
            "END;\n"
            "$$ LANGUAGE plpgsql"
        )

    # ------------------------------------------------------------------
    # UPSERT new-row detection
    # ------------------------------------------------------------------

    def upsert_is_new_expr(self) -> str:
        """SQL expression in a RETURNING clause to detect INSERT vs UPDATE.

        PostgreSQL and openGauss both expose the ``xmax`` system column.
        For a freshly inserted row ``xmax = 0``; an on-conflict update sets
        ``xmax`` to the current transaction id.
        """
        return "(xmax = 0) AS is_new"

    # ------------------------------------------------------------------
    # Connection helpers
    # ------------------------------------------------------------------

    def pool_server_settings(self) -> dict[str, str] | None:
        """Extra ``server_settings`` to pass to asyncpg ``create_pool``.

        openGauss may need compatibility-related session parameters.
        """
        if self.is_opengauss:
            return {"enable_thread_pool": "off"}
        return None

    def normalize_dsn(self, url: str) -> str:
        """Ensure the DSN uses the ``postgresql://`` scheme expected by asyncpg."""
        if url.startswith("postgresql+asyncpg://"):
            return "postgresql://" + url.removeprefix("postgresql+asyncpg://")
        if url.startswith("postgres://"):
            return "postgresql://" + url.removeprefix("postgres://")
        if url.startswith("opengauss://"):
            return "postgresql://" + url.removeprefix("opengauss://")
        return url
