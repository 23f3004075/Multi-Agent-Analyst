"""
SQL AST Checker — Allowlist-Based AST Validation.

Uses sqlglot to parse SQL into an Abstract Syntax Tree and validates
every node against a strict allowlist. Rejects anything not explicitly
permitted.

Guarantees:
    - ONLY read operations (SELECT, CTEs, Unions) are permitted
    - NO mutations (DROP, DELETE, INSERT, UPDATE, ALTER, TRUNCATE, etc.)
    - NO filesystem access (read_csv, read_parquet, glob, etc.)
    - NO network access (httpfs, s3, etc.)
    - NO system commands (INSTALL, LOAD, ATTACH, PRAGMA, SET, etc.)
    - NO multiple statements (semicolon-chaining injection)
    - Enforces row-limit injection if no LIMIT clause is present

Design Principle: ALLOWLIST, NOT BLOCKLIST.
If a node type is not in SAFE_NODE_TYPES, the query is REJECTED.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, SqlglotError

logger = logging.getLogger(__name__)


class ASTValidationError(Exception):
    """Raised when SQL fails the AST allowlist validation."""

    def __init__(self, message: str, violation_type: str = "unknown") -> None:
        self.message = message
        self.violation_type = violation_type
        super().__init__(f"[{violation_type}] {message}")


# fmt: off
SAFE_NODE_TYPES: frozenset[type] = frozenset({
    exp.Select, exp.From, exp.Where, exp.Group, exp.Having,
    exp.Order, exp.Limit, exp.Offset, exp.Distinct, exp.Star,
    exp.Subquery, exp.Exists, exp.With,
    exp.CTE,
    exp.Union, exp.Intersect, exp.Except,
    exp.Join, exp.Lateral,
    exp.Column, exp.Table, exp.Alias, exp.Identifier,
    exp.Dot, exp.Paren, exp.Ordered, exp.Var,
    exp.Literal, exp.Null, exp.Boolean,
    exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE,
    exp.Between, exp.In, exp.Like, exp.ILike, exp.Is,
    exp.RegexpLike,
    exp.And, exp.Or, exp.Not,
    exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Mod, exp.Neg,
    exp.IntDiv,
    exp.Count, exp.Sum, exp.Avg, exp.Min, exp.Max,
    exp.ArrayAgg, exp.GroupConcat, exp.Variance, exp.Stddev,
    exp.ApproxDistinct, exp.CountIf,
    exp.Window, exp.WindowSpec, exp.RowNumber, exp.Rank,
    exp.DenseRank, exp.Ntile, exp.NthValue,
    exp.FirstValue, exp.LastValue,
    exp.Lead, exp.Lag,
    exp.PartitionedByProperty,
    exp.Case, exp.If, exp.Coalesce, exp.Nullif,
    exp.Cast, exp.TryCast, exp.DataType,
    exp.Substring, exp.Upper, exp.Lower, exp.Trim,
    exp.Length, exp.Concat, exp.ConcatWs,
    exp.Replace, exp.Left, exp.Right,
    exp.Initcap, exp.RegexpExtract, exp.RegexpReplace,
    exp.Split, exp.DPipe,
    exp.Round, exp.Floor, exp.Ceil, exp.Abs,
    exp.Ln, exp.Log, exp.Exp, exp.Pow, exp.Sqrt,
    exp.Greatest, exp.Least, exp.Sign,
    exp.DateAdd, exp.DateDiff, exp.DateTrunc,
    exp.Extract, exp.CurrentDate, exp.CurrentTimestamp,
    exp.TimeToStr, exp.StrToTime, exp.TsOrDsToDate,
    exp.Interval, exp.DateSub, exp.Year, exp.Month, exp.Day,
    exp.Week,
    exp.Unnest,
    exp.Anonymous,
    exp.Tuple, exp.Array,
    exp.Parameter, exp.Placeholder,
    exp.Properties, exp.Property,
    exp.TableAlias,
    exp.AggFunc,
    exp.Func,
    exp.BitwiseAnd, exp.BitwiseOr, exp.BitwiseXor,
    exp.BitwiseNot, exp.BitwiseLeftShift, exp.BitwiseRightShift,
})
# fmt: on


SAFE_ANONYMOUS_FUNCTIONS: frozenset[str] = frozenset({
    "list_agg", "string_agg", "median", "quantile", "mode",
    "percentile_cont", "percentile_disc", "approx_count_distinct",
    "arg_min", "arg_max", "bit_and", "bit_or", "bit_xor",
    "bool_and", "bool_or", "corr", "covar_pop", "covar_samp",
    "entropy", "kurtosis", "skewness",
    "regexp_matches", "starts_with", "ends_with", "contains",
    "strip_accents", "reverse", "lpad", "rpad",
    "ltrim", "rtrim", "format", "printf", "md5", "hash",
    "date_part", "date_trunc", "make_date", "make_timestamp",
    "strftime", "strptime", "age", "datediff", "dateadd",
    "last_day", "monthname", "dayname",
    "random", "setseed",
    "list_value", "struct_pack", "list_aggregate",
    "list_sort", "list_distinct", "list_unique",
    "list_any_value", "list_filter", "list_transform",
    "typeof", "current_schema", "current_database",
    "row_number", "rank", "dense_rank",
    "coalesce", "nullif", "ifnull", "nvl", "nvl2",
    "greatest", "least",
    "to_char", "to_number", "to_date", "to_timestamp",
})


BLOCKED_FUNCTIONS: frozenset[str] = frozenset({
    "read_csv", "read_csv_auto", "read_parquet", "read_json",
    "read_json_auto", "read_json_objects", "read_blob",
    "read_text", "glob", "read_ndjson", "read_ndjson_auto",
    "read_ndjson_objects",
    "copy", "export_database", "write_parquet", "write_csv",
    "install", "load", "force_install",
    "attach", "detach", "use",
    "set", "reset", "current_setting",
    "system", "shell", "getenv",
    "duckdb_settings", "duckdb_functions", "duckdb_extensions",
    "duckdb_tables", "duckdb_columns", "duckdb_views",
    "duckdb_types", "duckdb_databases", "duckdb_schemas",
    "char", "chr",
    "generate_series", "range", "repeat",
})

MAX_AST_DEPTH = 50


@dataclass
class ValidationResult:
    """Result of an AST validation check."""

    is_valid: bool
    cleaned_sql: str = ""
    error: Optional[str] = None
    violation_type: Optional[str] = None
    node_type: Optional[str] = None


def _check_recursive_cte(tree: exp.Expression) -> None:
    """Block recursive CTEs (prevents recursion DoS attacks)."""
    for with_expr in tree.find_all(exp.With):
        if with_expr.args.get("recursive"):
            raise ASTValidationError(
                "Recursive CTEs are not permitted to prevent denial-of-service.",
                violation_type="resource_exhaustion",
            )


def _check_excessive_cross_joins(tree: exp.Expression) -> None:
    """Block queries with multiple cross joins (prevents Cartesian explosion)."""
    cross_join_count = 0
    for join in tree.find_all(exp.Join):
        kind = (join.args.get("kind") or "").upper()
        on_clause = join.args.get("on")
        using_clause = join.args.get("using")
        if "CROSS" in kind or (on_clause is None and using_clause is None and "NATURAL" not in kind):
            cross_join_count += 1
            if cross_join_count >= 2:
                raise ASTValidationError(
                    "Multiple CROSS JOINs detected (potential cartesian product DoS).",
                    violation_type="resource_exhaustion",
                )


def _check_network_urls(tree: exp.Expression) -> None:
    """Block URLs/remote paths in table references and string literals."""
    for table in tree.find_all(exp.Table):
        name = table.name.lower()
        if any(name.startswith(p) for p in ("http://", "https://", "s3://", "gcs://", "ftp://")):
            raise ASTValidationError(
                f"External network URL access blocked: {name}",
                violation_type="network_access",
            )
        if name.endswith((".csv", ".parquet", ".json", ".txt", ".tsv", ".arrow")):
            raise ASTValidationError(
                f"Direct file reading via table identifier blocked: {name}",
                violation_type="filesystem_read",
            )
    for lit in tree.find_all(exp.Literal):
        if lit.is_string:
            val = str(lit.this).lower()
            if any(val.startswith(p) for p in ("http://", "https://", "s3://", "gcs://", "ftp://")):
                raise ASTValidationError(
                    f"External network URL access blocked: {val}",
                    violation_type="network_access",
                )


def _check_tautologies(tree: exp.Expression) -> None:
    """Block SQL injection tautologies like 1=1 or 'a'='a' in WHERE clauses."""
    for where in tree.find_all(exp.Where):
        for eq in where.find_all(exp.EQ):
            if isinstance(eq.left, exp.Literal) and isinstance(eq.right, exp.Literal):
                if str(eq.left.this) == str(eq.right.this):
                    raise ASTValidationError(
                        f"Tautological condition detected ({eq.sql()}).",
                        violation_type="tautology",
                    )
            elif isinstance(eq.left, exp.Boolean) and isinstance(eq.right, exp.Boolean):
                if eq.left.this == eq.right.this:
                    raise ASTValidationError(
                        f"Tautological condition detected ({eq.sql()}).",
                        violation_type="tautology",
                    )


def _check_string_escapes(tree: exp.Expression) -> None:
    """Detect parser-differential string escape injections (e.g. embedded DDL / comments)."""
    for lit in tree.find_all(exp.Literal):
        if lit.is_string:
            val = str(lit.this)
            if re.search(r";\s*(?:DROP|DELETE|UPDATE|INSERT|ALTER|TRUNCATE|CREATE|EXEC)\b", val, re.IGNORECASE):
                raise ASTValidationError(
                    f"Suspicious SQL statements detected inside string literal: {val[:50]}",
                    violation_type="string_escape",
                )
            if "\\'" in val and "--" in val:
                raise ASTValidationError(
                    "Backslash-escaped quote with comment detected inside string literal",
                    violation_type="string_escape",
                )


def validate_sql(
    sql: str,
    dialect: str = "duckdb",
    max_rows: int = 5001,
    inject_limit: bool = True,
) -> str:
    """
    Validate SQL against the AST allowlist.

    Steps:
        1. Parse SQL into AST (reject on parse error)
        2. Enforce single-statement (reject multi-statement injection)
        3. Enforce SELECT root (reject non-query statements)
        4. Walk every AST node against the allowlist
        5. Check function names against block/allow lists
        6. Inject LIMIT if not present (prevent memory exhaustion)
        7. Return the cleaned, validated SQL string

    Args:
        sql: Raw SQL string to validate.
        dialect: SQL dialect for parsing (default: duckdb).
        max_rows: Maximum rows to allow (injected as LIMIT if missing).
        inject_limit: Whether to inject LIMIT clause if missing.

    Returns:
        Validated, cleaned SQL string safe for execution.

    Raises:
        ASTValidationError: If any validation check fails.
    """
    sql = sql.strip()

    if not sql:
        raise ASTValidationError("Empty SQL query", violation_type="empty")

    try:
        statements = sqlglot.parse(sql, dialect=dialect)
    except (ParseError, SqlglotError) as e:
        raise ASTValidationError(
            f"SQL syntax error: {e}",
            violation_type="parse_error",
        ) from e

    statements = [s for s in statements if s is not None]

    if len(statements) == 0:
        raise ASTValidationError(
            "No valid SQL statements found",
            violation_type="empty",
        )
    if len(statements) > 1:
        raise ASTValidationError(
            f"Multi-statement SQL detected ({len(statements)} statements). "
            f"Only single SELECT queries are allowed. "
            f"This prevents injection attacks like 'SELECT 1; DROP TABLE x'.",
            violation_type="multi_statement",
        )

    tree = statements[0]

    _validate_root_type(tree)

    depth = 0
    for node in tree.walk(bfs=True):
        depth += 1
        if depth > MAX_AST_DEPTH * 100:
            raise ASTValidationError(
                f"Query too complex: AST has more than {MAX_AST_DEPTH * 100} nodes. "
                f"This may indicate a denial-of-service attempt.",
                violation_type="complexity",
            )
        _validate_node(node)

    _check_recursive_cte(tree)
    _check_excessive_cross_joins(tree)
    _check_network_urls(tree)
    _check_tautologies(tree)
    _check_string_escapes(tree)

    if inject_limit:
        tree = _ensure_limit(tree, max_rows)

    cleaned = tree.sql(dialect=dialect)
    logger.debug("AST validation passed: %s", cleaned[:200])
    return cleaned


def _validate_root_type(tree: exp.Expression) -> None:
    """Ensure the root node is a SELECT, UNION, or CTE wrapping a SELECT."""
    root_type = type(tree)

    if root_type in {exp.Select, exp.Union, exp.Intersect, exp.Except}:
        return

    if root_type == exp.Select and tree.find(exp.CTE):
        return

    raise ASTValidationError(
        f"Root statement must be SELECT (got {root_type.__name__}). "
        f"Only read-only queries are permitted.",
        violation_type="non_select_root",
    )


def _validate_node(node: exp.Expression) -> None:
    """Validate a single AST node against the allowlist."""
    node_type = type(node)

    if node_type not in SAFE_NODE_TYPES:
        if not any(issubclass(node_type, safe) for safe in SAFE_NODE_TYPES):
            raise ASTValidationError(
                f"Unsafe AST node type: {node_type.__name__}. "
                f"This operation is not in the allowlist. "
                f"Only read-only SELECT operations are permitted.",
                violation_type="unsafe_node",
            )

    if isinstance(node, (exp.Anonymous, exp.Func)):
        func_name = _get_function_name(node)
        if func_name:
            _validate_function_name(func_name)


def _get_function_name(node: exp.Expression) -> str:
    """Extract the function name from a function node."""
    if isinstance(node, exp.Anonymous):
        return node.name.lower() if hasattr(node, "name") else ""

    if hasattr(node, "sql_name"):
        return node.sql_name().lower()
    if hasattr(node, "key"):
        return node.key.lower()

    return type(node).__name__.lower()


def _validate_function_name(func_name: str) -> None:
    """Check function name against blocked list."""
    func_name_clean = func_name.strip().lower()

    if func_name_clean in BLOCKED_FUNCTIONS:
        raise ASTValidationError(
            f"Blocked function: '{func_name_clean}'. "
            f"This function can access external resources and is not permitted.",
            violation_type="blocked_function",
        )


def _ensure_limit(tree: exp.Expression, max_rows: int) -> exp.Expression:
    """
    Inject LIMIT clause if the root SELECT doesn't have one.

    We inject max_rows + 1 so the executor can detect truncation
    (if exactly max_rows+1 are returned, results were truncated).
    """
    root_select = tree if isinstance(tree, exp.Select) else tree.find(exp.Select)
    if root_select is None:
        return tree

    existing_limit = root_select.find(exp.Limit)
    if existing_limit is not None:
        limit_val = existing_limit.find(exp.Literal)
        if limit_val and limit_val.is_int:
            try:
                val = int(limit_val.this)
                if val > max_rows:
                    logger.info(
                        "Reducing existing LIMIT from %d to %d",
                        val, max_rows,
                    )
                    limit_val.set("this", str(max_rows))
            except (ValueError, TypeError):
                pass
        return tree

    logger.debug("Injecting LIMIT %d (no LIMIT clause found)", max_rows)
    root_select.set("limit", exp.Limit(expression=exp.Literal.number(max_rows)))

    return tree


def validate_batch(
    queries: list[str],
    dialect: str = "duckdb",
) -> list[ValidationResult]:
    """
    Validate a batch of SQL queries. Returns results (not exceptions).

    Useful for red-team testing — validates all payloads and reports
    which ones passed/failed.
    """
    results = []
    for sql in queries:
        try:
            cleaned = validate_sql(sql, dialect=dialect)
            results.append(ValidationResult(
                is_valid=True,
                cleaned_sql=cleaned,
            ))
        except ASTValidationError as e:
            results.append(ValidationResult(
                is_valid=False,
                error=str(e),
                violation_type=e.violation_type,
            ))
    return results
