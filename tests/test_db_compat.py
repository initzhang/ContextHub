"""Tests for the database backend compatibility layer."""

import os

import pytest

from contexthub.db.compat import DatabaseBackend, DatabaseDialect


class TestDatabaseBackendEnum:
    def test_postgres_value(self):
        assert DatabaseBackend.POSTGRES == "postgres"

    def test_opengauss_value(self):
        assert DatabaseBackend.OPENGAUSS == "opengauss"

    def test_from_string(self):
        assert DatabaseBackend("postgres") == DatabaseBackend.POSTGRES
        assert DatabaseBackend("opengauss") == DatabaseBackend.OPENGAUSS


class TestDatabaseDialectPostgres:
    @pytest.fixture
    def dialect(self):
        return DatabaseDialect(DatabaseBackend.POSTGRES)

    def test_is_postgres(self, dialect):
        assert dialect.is_postgres is True
        assert dialect.is_opengauss is False

    def test_uuid_extension(self, dialect):
        assert "pgcrypto" in dialect.create_uuid_extension_sql()

    def test_uuid_default_uses_gen_random_uuid(self, dialect):
        assert dialect.uuid_generate_default() == "gen_random_uuid()"

    def test_vector_extension_uses_pgvector(self, dialect):
        sql = dialect.create_vector_extension_sql()
        assert sql is not None
        assert "vector" in sql
        assert "datavec" not in sql

    def test_hnsw_index(self, dialect):
        sql = dialect.hnsw_index_sql("idx_test", "my_table", "embedding_col")
        assert "CREATE INDEX idx_test" in sql
        assert "hnsw" in sql
        assert "my_table" in sql

    def test_notify_trigger(self, dialect):
        sql = dialect.notify_trigger_function_sql()
        assert "pg_notify" in sql
        assert "plpgsql" in sql

    def test_pool_server_settings_is_none(self, dialect):
        assert dialect.pool_server_settings() is None

    def test_normalize_dsn_postgresql(self, dialect):
        assert dialect.normalize_dsn("postgresql://u:p@h/d") == "postgresql://u:p@h/d"

    def test_normalize_dsn_postgres_scheme(self, dialect):
        assert dialect.normalize_dsn("postgres://u:p@h/d") == "postgresql://u:p@h/d"

    def test_normalize_dsn_asyncpg(self, dialect):
        assert dialect.normalize_dsn("postgresql+asyncpg://u:p@h/d") == "postgresql://u:p@h/d"


class TestDatabaseDialectOpenGauss:
    @pytest.fixture
    def dialect(self):
        return DatabaseDialect(DatabaseBackend.OPENGAUSS)

    def test_is_opengauss(self, dialect):
        assert dialect.is_opengauss is True
        assert dialect.is_postgres is False

    def test_uuid_extension(self, dialect):
        sql = dialect.create_uuid_extension_sql()
        assert "uuid-ossp" in sql

    def test_uuid_default_uses_uuid_generate_v4(self, dialect):
        assert dialect.uuid_generate_default() == "uuid_generate_v4()"

    def test_vector_extension_is_none_builtin(self, dialect):
        assert dialect.create_vector_extension_sql() is None

    def test_hnsw_index(self, dialect):
        sql = dialect.hnsw_index_sql("idx_test", "my_table", "embedding_col")
        assert "CREATE INDEX idx_test" in sql
        assert "hnsw" in sql

    def test_pool_server_settings(self, dialect):
        settings = dialect.pool_server_settings()
        assert settings is not None
        assert isinstance(settings, dict)

    def test_normalize_dsn_opengauss_scheme(self, dialect):
        assert dialect.normalize_dsn("opengauss://u:p@h/d") == "postgresql://u:p@h/d"

    def test_normalize_dsn_postgresql_untouched(self, dialect):
        assert dialect.normalize_dsn("postgresql://u:p@h/d") == "postgresql://u:p@h/d"

    def test_upsert_is_new_expr(self, dialect):
        expr = dialect.upsert_is_new_expr()
        assert "xmax" in expr
        assert "is_new" in expr


class TestSettingsIntegration:
    def test_default_backend_is_postgres(self):
        from contexthub.config import Settings
        s = Settings()
        assert s.db_backend == DatabaseBackend.POSTGRES
        assert s.dialect.is_postgres

    def test_opengauss_backend_from_env(self, monkeypatch):
        monkeypatch.setenv("DB_BACKEND", "opengauss")
        from contexthub.config import Settings
        s = Settings()
        assert s.db_backend == DatabaseBackend.OPENGAUSS
        assert s.dialect.is_opengauss
