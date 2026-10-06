from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Generator

import duckdb

from src.config import Settings

logger = logging.getLogger(__name__)


class DatabaseConnectionError(Exception):
    pass


def create_secure_connection(settings: Settings) -> duckdb.DuckDBPyConnection:
    db_path = str(settings.database_path)

    try:
        conn = duckdb.connect(database=db_path, read_only=True)
        logger.info("DuckDB connection opened (read_only=True): %s", db_path)

    except duckdb.IOException as e:
        raise DatabaseConnectionError(
            f"Cannot open database at {db_path}. "
            f"Run 'make seed' to create it. Error: {e}"
        ) from e

    try:
        is_locked = conn.execute("SELECT current_setting('lock_configuration')").fetchone()[0]
        if str(is_locked).lower() == "true":
            logger.debug("DuckDB configuration already locked for active database instance")
            return conn

        conn.execute("SET enable_external_access = false")
        logger.debug("External access disabled")

        conn.execute(f"SET memory_limit = '{settings.db_memory_limit}'")
        conn.execute(f"SET threads = {settings.db_max_threads}")
        logger.debug(
            "Resource limits set: memory=%s, threads=%d",
            settings.db_memory_limit,
            settings.db_max_threads,
        )

        conn.execute("SET lock_configuration = true")
        logger.debug("Configuration locked")

    except duckdb.Error as e:
        conn.close()
        raise DatabaseConnectionError(
            f"Failed to apply security configuration: {e}"
        ) from e

    _verify_lockdown(conn)

    return conn


def _verify_lockdown(conn: duckdb.DuckDBPyConnection) -> None:
    checks = {
        "enable_external_access": "false",
    }

    for setting, expected in checks.items():
        try:
            result = conn.execute(
                f"SELECT current_setting('{setting}')"
            ).fetchone()
            actual = str(result[0]).lower() if result else "UNKNOWN"
            if actual != expected:
                raise DatabaseConnectionError(
                    f"Security verification failed: {setting} is '{actual}', "
                    f"expected '{expected}'. Aborting."
                )
        except duckdb.Error:
            raise DatabaseConnectionError(
                f"Cannot verify security setting '{setting}'. "
                f"DuckDB version may be incompatible."
            )

    logger.info("Security lockdown verified successfully")


class ConnectionManager:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._conn: duckdb.DuckDBPyConnection | None = None

    def __enter__(self) -> duckdb.DuckDBPyConnection:
        self._conn = create_secure_connection(self._settings)
        return self._conn

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:  # noqa: ANN001
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                logger.warning("Error closing DuckDB connection", exc_info=True)
            self._conn = None

    @property
    def connection(self) -> duckdb.DuckDBPyConnection:
        if self._conn is None:
            raise DatabaseConnectionError("Connection not established. Use 'with' statement.")
        return self._conn


@contextmanager
def get_connection(settings: Settings) -> Generator[duckdb.DuckDBPyConnection, None, None]:
    with ConnectionManager(settings) as conn:
        yield conn
