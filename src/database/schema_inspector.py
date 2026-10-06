from __future__ import annotations

import logging
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Optional

import duckdb

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ColumnInfo:
    name: str
    data_type: str
    is_nullable: bool
    is_primary_key: bool = False
    sample_values: list[Any] = field(default_factory=list)
    distinct_count: int = 0
    null_percentage: float = 0.0
    description: str = ""

    def to_ddl_fragment(self) -> str:
        parts = [f"    {self.name} {self.data_type}"]
        if self.is_primary_key:
            parts.append("PRIMARY KEY")
        elif not self.is_nullable:
            parts.append("NOT NULL")
        return " ".join(parts)

    def to_embedding_text(self) -> str:
        text = f"{self.name} ({self.data_type})"
        if self.description:
            text += f" — {self.description}"
        if self.sample_values:
            samples = ", ".join(str(v) for v in self.sample_values[:3])
            text += f" [e.g., {samples}]"
        return text


@dataclass(frozen=True)
class TableInfo:
    name: str
    columns: list[ColumnInfo]
    row_count: int
    description: str = ""

    @property
    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]

    @property
    def primary_keys(self) -> list[str]:
        return [c.name for c in self.columns if c.is_primary_key]

    def to_ddl(self) -> str:
        col_defs = ",\n".join(c.to_ddl_fragment() for c in self.columns)
        return f"CREATE TABLE {self.name} (\n{col_defs}\n);"

    def to_embedding_text(self) -> str:
        lines = [f"Table: {self.name}"]
        if self.description:
            lines.append(f"Description: {self.description}")
        lines.append(f"Rows: {self.row_count:,}")
        lines.append("Columns:")
        for col in self.columns:
            lines.append(f"  - {col.to_embedding_text()}")
        return "\n".join(lines)

    def to_prompt_context(self) -> str:
        ddl = self.to_ddl()
        return f"-- {self.name} ({self.row_count:,} rows)\n{ddl}"


class SchemaInspector:
    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self._conn = conn
        self._cache: dict[str, TableInfo] = {}

    def get_all_table_names(self) -> list[str]:
        result = self._conn.execute("""
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'main'
              AND table_type = 'BASE TABLE'
            ORDER BY table_name
        """).fetchall()
        return [row[0] for row in result]

    def get_table_metadata(self, table_name: str) -> TableInfo:
        if table_name in self._cache:
            return self._cache[table_name]

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

        pk_columns = self._get_primary_keys(table_name)

        row_count = self._conn.execute(
            f"SELECT COUNT(*) FROM {table_name}"
        ).fetchone()[0]

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
        return [
            self.get_table_metadata(name)
            for name in self.get_all_table_names()
        ]

    def get_full_ddl(self, table_names: list[str] | None = None) -> str:
        if table_names is None:
            table_names = self.get_all_table_names()

        ddl_parts = []
        for name in table_names:
            table_info = self.get_table_metadata(name)
            ddl_parts.append(table_info.to_prompt_context())

        return "\n\n".join(ddl_parts)

    def get_embedding_texts(self) -> dict[str, str]:
        return {
            table.name: table.to_embedding_text()
            for table in self.get_all_tables()
        }

    def _get_primary_keys(self, table_name: str) -> set[str]:
        try:
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
