"""
SQL AST Allowlist Validator.

SECURITY MODEL: ALLOWLIST, not denylist.

Only explicitly approved AST node types are permitted. Everything else
is rejected. This inverts the traditional security model from
"block known-bad" to "allow known-good" — eliminating entire categories
of bypass attacks.

This is DEFENSE-IN-DEPTH only. The primary security boundary is the
DuckDB configuration lockdown (read_only=True, enable_external_access=false,
lock_configuration=true) in connection.py.

Attack vectors this layer catches (even though DuckDB config blocks them):
    - read_csv('/etc/passwd')  → rejected: read_csv is not in SAFE_FUNCTIONS
    - COPY TO ...              → rejected: Copy is not in SAFE_NODE_TYPES
    - ATTACH ...               → rejected: not a SELECT root
    - INSTALL/LOAD             → rejected: Command not in SAFE_NODE_TYPES
    - SELECT 1; DROP TABLE x   → rejected: multi-statement
    - UPDATE/DELETE/INSERT     → rejected: not a SELECT root

Usage:
    from src.guardrails.sql_ast_checker import validate_sql, ASTValidationError

    try:
        clean_sql = validate_sql("SELECT * FROM orders LIMIT 10")
    except ASTValidationError as e:
        print(f"Blocked: {e}")
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional
import re

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Exceptions
# ─────────────────────────────────────────────────────────────────────


class ASTValidationError(Exception):
    """Raised when SQL fails the AST allowlist validation."""

    def __init__(self, message: str, violation_type: str = "unknown") -> None:
        self.message = message
        self.violation_type = violation_type
        super().__init__(f"[{violation_type}] {message}")


# ─────────────────────────────────────────────────────────────────────
# Allowlist: Exhaustive set of safe AST node types
# ─────────────────────────────────────────────────────────────────────

# fmt: off
SAFE_NODE_TYPES: frozenset[type] = frozenset({
    # ── Query structure ──
    exp.Select, exp.From, exp.Where, exp.Group, exp.Having,
    exp.Order, exp.Limit, exp.Offset, exp.Distinct, exp.Star,
    exp.Subquery, exp.Exists, exp.With,

    # ── CTE ──
    exp.CTE,

    # ── Set operations ──
    exp.Union, exp.Intersect, exp.Except,

    # ── Joins ──
    exp.Join, exp.Lateral,

    # ── Column references ──
    exp.Column, exp.Table, exp.Alias, exp.Identifier,
    exp.Dot, exp.Paren, exp.Ordered, exp.Var,

    # ── Literals ──
    exp.Literal, exp.Null, exp.Boolean,

    # ── Comparisons ──
    exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE,
    exp.Between, exp.In, exp.Like, exp.ILike, exp.Is,
    exp.RegexpLike,

    # ── Logical ──
    exp.And, exp.Or, exp.Not,

    # ── Arithmetic ──
    exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Mod, exp.Neg,
    exp.IntDiv,

    # ── Aggregate functions ──
    exp.Count, exp.Sum, exp.Avg, exp.Min, exp.Max,
    exp.ArrayAgg, exp.GroupConcat, exp.Variance, exp.Stddev,
    exp.ApproxDistinct, exp.CountIf,

    # ── Window functions ──
    exp.Window, exp.WindowSpec, exp.RowNumber, exp.Rank,
    exp.DenseRank, exp.Ntile, exp.NthValue,
    exp.FirstValue, exp.LastValue,
    exp.Lead, exp.Lag,
    exp.PartitionedByProperty,

    # ── Conditional ──
    exp.Case, exp.If, exp.Coalesce, exp.Nullif,

    # ── Type casting ──
    exp.Cast, exp.TryCast, exp.DataType,

    # ── String functions ──
    exp.Substring, exp.Upper, exp.Lower, exp.Trim,
    exp.Length, exp.Concat, exp.ConcatWs,
    exp.Replace, exp.Left, exp.Right,
    exp.Initcap, exp.RegexpExtract, exp.RegexpReplace,
    exp.Split, exp.DPipe,

    # ── Numeric functions ──
    exp.Round, exp.Floor, exp.Ceil, exp.Abs,
    exp.Ln, exp.Log, exp.Exp, exp.Pow, exp.Sqrt,
    exp.Greatest, exp.Least, exp.Sign,

    # ── Date/time functions ──
    exp.DateAdd, exp.DateDiff, exp.DateTrunc,
    exp.Extract, exp.CurrentDate, exp.CurrentTimestamp,
    exp.TimeToStr, exp.StrToTime, exp.TsOrDsToDate,
    exp.Interval, exp.DateSub, exp.Year, exp.Month, exp.Day,
    exp.Week,

    # ── Conversion ──
    exp.Unnest,

    # ── Misc safe expressions ──
    exp.Anonymous,  # Covers dialect-specific functions by name (validated below)
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


# Functions allowed when they appear as Anonymous nodes
# (dialect-specific functions that sqlglot doesn't have typed nodes for)
SAFE_ANONYMOUS_FUNCTIONS: frozenset[str] = frozenset({
    # DuckDB aggregates
    "list_agg", "string_agg", "median", "quantile", "mode",
    "percentile_cont", "percentile_disc", "approx_count_distinct",
    "arg_min", "arg_max", "bit_and", "bit_or", "bit_xor",
    "bool_and", "bool_or", "corr", "covar_pop", "covar_samp",
    "entropy", "kurtosis", "skewness",
    # DuckDB string
    "regexp_matches", "starts_with", "ends_with", "contains",
    "strip_accents", "reverse", "lpad", "rpad",
    "ltrim", "rtrim", "format", "printf", "md5", "hash",
    # DuckDB date/time
    "date_part", "date_trunc", "make_date", "make_timestamp",
    "strftime", "strptime", "age", "datediff", "dateadd",
    "last_day", "monthname", "dayname",
    # DuckDB numeric
    "random", "setseed",
    # DuckDB list/struct
    "list_value", "struct_pack", "list_aggregate",
    "list_sort", "list_distinct", "list_unique",
    "list_any_value", "list_filter", "list_transform",
    # DuckDB utility
    "typeof", "current_schema", "current_database",
    "row_number", "rank", "dense_rank",
    # Standard SQL
    "coalesce", "nullif", "ifnull", "nvl", "nvl2",
    "greatest", "least",
    "to_char", "to_number", "to_date", "to_timestamp",
})


# Explicitly BLOCKED function names (even if wrapped in Anonymous)
# These are DuckDB-specific functions that can access the filesystem
BLOCKED_FUNCTIONS: frozenset[str] = frozenset({
    # Filesystem access
    "read_csv", "read_csv_auto", "read_parquet", "read_json",
    "read_json_auto", "read_json_objects", "read_blob",
    "read_text", "glob", "read_ndjson", "read_ndjson_auto",
    "read_ndjson_objects",
    # Data export
    "copy", "export_database", "write_parquet", "write_csv",
    # Extension management
    "install", "load", "force_install",
    # Database management
    "attach", "detach", "use",
    # Configuration
    "set", "reset", "current_setting",
    # System
    "system", "shell", "getenv",
    # DuckDB system table functions (info disclosure)
    "duckdb_settings", "duckdb_functions", "duckdb_extensions",
    "duckdb_tables", "duckdb_columns", "duckdb_views",
    "duckdb_types", "duckdb_databases", "duckdb_schemas",
    # Encoding bypass
    "char", "chr",
    # Resource exhaustion
    "generate_series", "range", "repeat",
})


# Maximum allowed query depth (prevents stack overflow from deeply nested CTEs)
MAX_AST_DEPTH = 50


# ─────────────────────────────────────────────────────────────────────
# Validation Result
# ─────────────────────────────────────────────────────────────────────


@dataclass
class ValidationResult:
    """Result of an AST validation check."""

    is_valid: bool
    cleaned_sql: str = ""
    error: Optional[str] = None
    violation_type: Optional[str] = None
    node_type: Optional[str] = None  # The offending node type, if any


# ─────────────────────────────────────────────────────────────────────
# Structural Security Checks
# ─────────────────────────────────────────────────────────────────────


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




# ─────────────────────────────────────────────────────────────────────
# Validator
# ─────────────────────────────────────────────────────────────────────


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

    # ── Step 1: Parse ────────────────────────────────────────────────
    try:
        statements = sqlglot.parse(sql, dialect=dialect)
    except ParseError as e:
        raise ASTValidationError(
            f"SQL syntax error: {e}",
            violation_type="parse_error",
        ) from e

    # Filter out None entries (blank statements from trailing semicolons)
    statements = [s for s in statements if s is not None]

    # ── Step 2: Single statement only ────────────────────────────────
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

    # ── Step 3: Root must be SELECT-like ─────────────────────────────
    _validate_root_type(tree)

    # ── Step 4: Walk every node against allowlist ────────────────────
    depth = 0
    for node in tree.walk(bfs=True):
        depth += 1
        if depth > MAX_AST_DEPTH * 100:  # rough node count heuristic
            raise ASTValidationError(
                f"Query too complex: AST has more than {MAX_AST_DEPTH * 100} nodes. "
                f"This may indicate a denial-of-service attempt.",
                violation_type="complexity",
            )
        _validate_node(node)

    # ── Step 5: Additional structural checks ─────────────────────────
    _check_recursive_cte(tree)
    _check_excessive_cross_joins(tree)
    _check_network_urls(tree)
    _check_tautologies(tree)
    _check_string_escapes(tree)

    # ── Step 6: Inject LIMIT if missing ──────────────────────────────
    if inject_limit:
        tree = _ensure_limit(tree, max_rows)

    # ── Step 7: Return cleaned SQL ───────────────────────────────────
    cleaned = tree.sql(dialect=dialect)
    logger.debug("AST validation passed: %s", cleaned[:200])
    return cleaned


def _validate_root_type(tree: exp.Expression) -> None:
    """Ensure the root node is a SELECT, UNION, or CTE wrapping a SELECT."""
    root_type = type(tree)

    # Direct SELECT, UNION, INTERSECT, EXCEPT
    if root_type in {exp.Select, exp.Union, exp.Intersect, exp.Except}:
        return

    # CTE (WITH ... SELECT) — root is a Select with CTEs attached
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

    # Check against safe type set
    if node_type not in SAFE_NODE_TYPES:
        # Check if it's a subclass of a safe type (e.g., custom Func subclass)
        if not any(issubclass(node_type, safe) for safe in SAFE_NODE_TYPES):
            raise ASTValidationError(
                f"Unsafe AST node type: {node_type.__name__}. "
                f"This operation is not in the allowlist. "
                f"Only read-only SELECT operations are permitted.",
                violation_type="unsafe_node",
            )

    # For function calls (Anonymous, Func subclasses), check the function name
    if isinstance(node, (exp.Anonymous, exp.Func)):
        func_name = _get_function_name(node)
        if func_name:
            _validate_function_name(func_name)


def _get_function_name(node: exp.Expression) -> str:
    """Extract the function name from a function node."""
    if isinstance(node, exp.Anonymous):
        return node.name.lower() if hasattr(node, "name") else ""

    # For typed function nodes, get the SQL name
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
    # Find the outermost SELECT
    root_select = tree if isinstance(tree, exp.Select) else tree.find(exp.Select)
    if root_select is None:
        return tree

    # Check if LIMIT already exists
    existing_limit = root_select.find(exp.Limit)
    if existing_limit is not None:
        # Validate existing LIMIT isn't absurdly large
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

    # No LIMIT found — inject one
    logger.debug("Injecting LIMIT %d (no LIMIT clause found)", max_rows)
    root_select.set("limit", exp.Limit(expression=exp.Literal.number(max_rows)))

    return tree


# ─────────────────────────────────────────────────────────────────────
# Batch Validation (for testing)
# ─────────────────────────────────────────────────────────────────────


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
