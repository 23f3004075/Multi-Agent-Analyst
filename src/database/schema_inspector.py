"""
Database Schema Inspector.

Extracts DDL definitions, column metadata, sample values, and table
statistics from a DuckDB database. This metadata is used by:
    - Schema linker (embedding-based table retrieval)
    - SQL generation prompts (DDL context for the LLM)
    - Semantic layer (column descriptions for metric mapping)

The inspector reads schema once and caches it. It does NOT require
a security-hardened connection since it only reads metadata.

Usage:
    from src.database.schema_inspector import SchemaInspector

    inspector = SchemaInspector(conn)
    tables = inspector.get_all_tables()
    ddl = inspector.get_ddl("orders")
    metadata = inspector.get_table_metadata("orders")
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Optional

import duckdb

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ColumnInfo:
    """Metadata for a single database column."""

    name: str
    data_type: str
    is_nullable: bool
    is_primary_key: bool = False
    sample_values: list[Any] = field(default_factory=list)
    distinct_count: int = 0
    null_percentage: float = 0.0
    description: str = ""  # Populated by semantic layer

    def to_ddl_fragment(self) -> str:
        """Format as a DDL column definition."""
        parts = [f"    {self.name} {self.data_type}"]
        if self.is_primary_key:
            parts.append("PRIMARY KEY")
        elif not self.is_nullable:
            parts.append("NOT NULL")
        return " ".join(parts)

    def to_embedding_text(self) -> str:
        """Format for embedding — includes samples for semantic matching."""
        text = f"{self.name} ({self.data_type})"
        if self.description:
            text += f" — {self.description}"
        if self.sample_values:
            samples = ", ".join(str(v) for v in self.sample_values[:3])
            text += f" [e.g., {samples}]"
        return text


@dataclass(frozen=True)
class TableInfo:
    """Complete metadata for a database table."""

    name: str
    columns: list[ColumnInfo]
    row_count: int
    description: str = ""  # Populated by semantic layer

    @property
    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]

    @property
    def primary_keys(self) -> list[str]:
        return [c.name for c in self.columns if c.is_primary_key]

    def to_ddl(self) -> str:
        """Generate CREATE TABLE DDL string."""
        col_defs = ",\n".join(c.to_ddl_fragment() for c in self.columns)
        return f"CREATE TABLE {self.name} (\n{col_defs}\n);"

    def to_embedding_text(self) -> str:
        """
        Format for embedding-based retrieval.

        Includes table name, description, column names with types and
        sample values. This gives the embedding model rich semantic
        signal for matching user queries to relevant tables.
        """
        lines = [f"Table: {self.name}"]
        if self.description:
            lines.append(f"Description: {self.description}")
        lines.append(f"Rows: {self.row_count:,}")
        lines.append("Columns:")
        for col in self.columns:
            lines.append(f"  - {col.to_embedding_text()}")
        return "\n".join(lines)

    def to_prompt_context(self) -> str:
        """
        Format for LLM prompt injection.

        Concise DDL + row count. Used in SQL generation prompts.
        """
        ddl = self.to_ddl()
        return f"-- {self.name} ({self.row_count:,} rows)\n{ddl}"


class SchemaInspector:
    """
    Inspects and caches DuckDB schema metadata.

    Reads table structures, column types, sample values, and statistics.
    All reads are performed once and cached for the session lifetime.
    """

    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self._conn = conn
        self._cache: dict[str, TableInfo] = {}

    def get_all_table_names(self) -> list[str]:
        """Return sorted list of all user table names."""
        result = self._conn.execute("""
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'main'
              AND table_type = 'BASE TABLE'
            ORDER BY table_name
        """).fetchall()
        return [row[0] for row in result]

    def get_table_metadata(self, table_name: str) -> TableInfo:
        """
        Get full metadata for a table, including column info and samples.

        Results are cached — safe to call repeatedly.
        """
        if table_name in self._cache:
            return self._cache[table_name]

        # Get column metadata
        columns_raw = self._conn.execute(f"""
            SELECT
                column_name,
                data_type,
                is_nullable
            FROM information_schema.columns
            WHERE table_schema = 'main'
              AND table_name = '{table_name}'
            ORDER BY ordinal_position
        """).fetchall()

        if not columns_raw:
            raise ValueError(f"Table '{table_name}' not found in schema")

        # Get primary key columns
        pk_columns = self._get_primary_keys(table_name)

        # Get row count
        row_count = self._conn.execute(
            f"SELECT COUNT(*) FROM {table_name}"
        ).fetchone()[0]

        # Build column info with sample values
        columns = []
        for col_name, data_type, is_nullable in columns_raw:
            sample_values = self._get_sample_values(table_name, col_name)
            distinct_count = self._get_distinct_count(table_name, col_name)
            null_pct = self._get_null_percentage(table_name, col_name, row_count)

            columns.append(ColumnInfo(
                name=col_name,
                data_type=data_type,
                is_nullable=(is_nullable == "YES"),
                is_primary_key=(col_name in pk_columns),
                sample_values=sample_values,
                distinct_count=distinct_count,
                null_percentage=null_pct,
            ))

        table_info = TableInfo(
            name=table_name,
            columns=columns,
            row_count=row_count,
        )

        self._cache[table_name] = table_info
        logger.debug(
            "Inspected table %s: %d columns, %d rows",
            table_name, len(columns), row_count,
        )

        return table_info

    def get_all_tables(self) -> list[TableInfo]:
        """Get metadata for all tables in the database."""
        return [
            self.get_table_metadata(name)
            for name in self.get_all_table_names()
        ]

    def get_full_ddl(self, table_names: list[str] | None = None) -> str:
        """
        Get combined DDL for specified tables (or all tables).

        Used by SQL generation prompts when schema linking retrieves
        a subset of relevant tables.
        """
        if table_names is None:
            table_names = self.get_all_table_names()

        ddl_parts = []
        for name in table_names:
            table_info = self.get_table_metadata(name)
            ddl_parts.append(table_info.to_prompt_context())

        return "\n\n".join(ddl_parts)

    def get_embedding_texts(self) -> dict[str, str]:
        """
        Get embedding-ready text representations for all tables.

        Returns:
            Dict mapping table_name → rich text description including
            columns, types, sample values, and row counts.
        """
        return {
            table.name: table.to_embedding_text()
            for table in self.get_all_tables()
        }

    # ── Private helpers ──────────────────────────────────────────────

    def _get_primary_keys(self, table_name: str) -> set[str]:
        """Extract primary key column names for a table."""
        try:
            # DuckDB-specific: query constraint info
            result = self._conn.execute(f"""
                SELECT column_name
                FROM duckdb_constraints()
                WHERE table_name = '{table_name}'
                  AND constraint_type = 'PRIMARY KEY'
            """).fetchall()
            return {row[0] for row in result}
        except duckdb.Error:
            return set()

    def _get_sample_values(
        self, table_name: str, column_name: str, n: int = 5
    ) -> list[Any]:
        """Get N distinct non-null sample values from a column."""
        try:
            result = self._conn.execute(f"""
                SELECT DISTINCT "{column_name}"
                FROM {table_name}
                WHERE "{column_name}" IS NOT NULL
                LIMIT {n}
            """).fetchall()
            return [row[0] for row in result]
        except duckdb.Error:
            return []

    def _get_distinct_count(self, table_name: str, column_name: str) -> int:
        """Get approximate distinct value count for a column."""
        try:
            result = self._conn.execute(f"""
                SELECT approx_count_distinct("{column_name}")
                FROM {table_name}
            """).fetchone()
            return result[0] if result else 0
        except duckdb.Error:
            return 0

    def _get_null_percentage(
        self, table_name: str, column_name: str, total_rows: int
    ) -> float:
        """Get percentage of null values in a column."""
        if total_rows == 0:
            return 0.0
        try:
            result = self._conn.execute(f"""
                SELECT COUNT(*) FILTER (WHERE "{column_name}" IS NULL)
                FROM {table_name}
            """).fetchone()
            null_count = result[0] if result else 0
            return round(null_count / total_rows * 100, 1)
        except duckdb.Error:
            return 0.0
