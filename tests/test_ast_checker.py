"""
Tests for the AST Allowlist Validator.

Covers:
    - Valid SQL queries pass validation
    - All mutation operations are blocked (DROP, DELETE, UPDATE, INSERT, ALTER)
    - DuckDB filesystem functions blocked (read_csv, read_parquet, glob, etc.)
    - Multi-statement injection blocked
    - LIMIT injection works correctly
    - Edge cases (empty, comments, unicode, deeply nested)
"""

from __future__ import annotations

import pytest

from src.guardrails.sql_ast_checker import (
    ASTValidationError,
    validate_sql,
    validate_batch,
)


# ─────────────────────────────────────────────────────────────────────
# Valid queries — these MUST pass
# ─────────────────────────────────────────────────────────────────────


class TestValidQueries:
    """Ensure legitimate analytical queries pass validation."""

    def test_simple_select(self) -> None:
        result = validate_sql("SELECT * FROM orders LIMIT 10")
        assert "orders" in result
        assert "LIMIT" in result

    def test_select_with_where(self) -> None:
        result = validate_sql(
            "SELECT customer_id, order_status FROM orders WHERE order_status = 'delivered'"
        )
        assert "customer_id" in result

    def test_aggregation(self) -> None:
        result = validate_sql(
            "SELECT COUNT(*), AVG(price) FROM order_items GROUP BY product_id"
        )
        assert "COUNT" in result
        assert "AVG" in result

    def test_join(self) -> None:
        result = validate_sql("""
            SELECT o.order_id, c.customer_city
            FROM orders o
            JOIN customers c ON o.customer_id = c.customer_id
            LIMIT 100
        """)
        assert "JOIN" in result

    def test_multi_table_join(self) -> None:
        result = validate_sql("""
            SELECT o.order_id, oi.price, p.product_category_name
            FROM orders o
            JOIN order_items oi ON o.order_id = oi.order_id
            JOIN products p ON oi.product_id = p.product_id
            WHERE o.order_status = 'delivered'
            LIMIT 50
        """)
        assert "product_category_name" in result

    def test_subquery(self) -> None:
        result = validate_sql("""
            SELECT * FROM orders
            WHERE customer_id IN (
                SELECT customer_id FROM customers
                WHERE customer_state = 'SP'
            )
            LIMIT 100
        """)
        assert "customer_state" in result

    def test_cte(self) -> None:
        result = validate_sql("""
            WITH top_products AS (
                SELECT product_id, SUM(price) as total_revenue
                FROM order_items
                GROUP BY product_id
                ORDER BY total_revenue DESC
                LIMIT 10
            )
            SELECT tp.product_id, tp.total_revenue, p.product_category_name
            FROM top_products tp
            JOIN products p ON tp.product_id = p.product_id
        """)
        assert "top_products" in result

    def test_window_function(self) -> None:
        result = validate_sql("""
            SELECT
                customer_id,
                order_id,
                ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY order_purchase_timestamp) as order_num
            FROM orders
            LIMIT 100
        """)
        assert "ROW_NUMBER" in result

    def test_case_when(self) -> None:
        result = validate_sql("""
            SELECT
                order_id,
                CASE
                    WHEN price > 100 THEN 'high'
                    WHEN price > 50 THEN 'medium'
                    ELSE 'low'
                END as price_tier
            FROM order_items
            LIMIT 100
        """)
        assert "CASE" in result

    def test_date_functions(self) -> None:
        result = validate_sql("""
            SELECT
                DATE_TRUNC('month', order_purchase_timestamp) as month,
                COUNT(*) as order_count
            FROM orders
            GROUP BY 1
            ORDER BY 1
        """)
        assert "DATE_TRUNC" in result.upper() or "date_trunc" in result

    def test_having_clause(self) -> None:
        result = validate_sql("""
            SELECT customer_id, COUNT(*) as order_count
            FROM orders
            GROUP BY customer_id
            HAVING COUNT(*) > 5
            ORDER BY order_count DESC
            LIMIT 20
        """)
        assert "HAVING" in result

    def test_union(self) -> None:
        result = validate_sql("""
            SELECT customer_city, 'customer' as type FROM customers LIMIT 5
            UNION ALL
            SELECT seller_city, 'seller' as type FROM sellers LIMIT 5
        """)
        assert "UNION" in result

    def test_coalesce_and_nullif(self) -> None:
        result = validate_sql(
            "SELECT COALESCE(product_category_name, 'unknown') FROM products LIMIT 10"
        )
        assert "COALESCE" in result

    def test_string_functions(self) -> None:
        result = validate_sql(
            "SELECT UPPER(customer_city), LENGTH(customer_id) FROM customers LIMIT 10"
        )
        assert "UPPER" in result


# ─────────────────────────────────────────────────────────────────────
# Blocked mutations — these MUST fail
# ─────────────────────────────────────────────────────────────────────


class TestBlockedMutations:
    """Ensure all write operations are blocked."""

    def test_drop_table(self) -> None:
        with pytest.raises(ASTValidationError, match="non_select_root|unsafe_node"):
            validate_sql("DROP TABLE orders")

    def test_delete(self) -> None:
        with pytest.raises(ASTValidationError, match="non_select_root|unsafe_node"):
            validate_sql("DELETE FROM orders WHERE 1=1")

    def test_update(self) -> None:
        with pytest.raises(ASTValidationError, match="non_select_root|unsafe_node"):
            validate_sql("UPDATE orders SET order_status = 'cancelled'")

    def test_insert(self) -> None:
        with pytest.raises(ASTValidationError, match="non_select_root|unsafe_node"):
            validate_sql("INSERT INTO orders VALUES ('a','b','c','d','e','f','g','h')")

    def test_alter_table(self) -> None:
        with pytest.raises(ASTValidationError, match="non_select_root|unsafe_node"):
            validate_sql("ALTER TABLE orders ADD COLUMN pwned TEXT")

    def test_truncate(self) -> None:
        with pytest.raises(ASTValidationError):
            validate_sql("TRUNCATE TABLE orders")

    def test_create_table(self) -> None:
        with pytest.raises(ASTValidationError):
            validate_sql("CREATE TABLE evil (id INT)")

    def test_create_view(self) -> None:
        with pytest.raises(ASTValidationError):
            validate_sql("CREATE VIEW evil_view AS SELECT * FROM orders")

    def test_create_macro(self) -> None:
        with pytest.raises(ASTValidationError):
            validate_sql("CREATE MACRO evil(x) AS x + 1")


# ─────────────────────────────────────────────────────────────────────
# Blocked filesystem access — these MUST fail
# ─────────────────────────────────────────────────────────────────────


class TestBlockedFilesystem:
    """Ensure DuckDB filesystem functions are blocked."""

    def test_read_csv(self) -> None:
        with pytest.raises(ASTValidationError, match="blocked_function|unsafe_node"):
            validate_sql("SELECT * FROM read_csv('/etc/passwd')")

    def test_read_csv_auto(self) -> None:
        with pytest.raises(ASTValidationError, match="blocked_function|unsafe_node"):
            validate_sql("SELECT * FROM read_csv_auto('/etc/passwd')")

    def test_read_parquet(self) -> None:
        with pytest.raises(ASTValidationError, match="blocked_function|unsafe_node"):
            validate_sql("SELECT * FROM read_parquet('/data/*.parquet')")

    def test_read_json(self) -> None:
        with pytest.raises(ASTValidationError, match="blocked_function|unsafe_node"):
            validate_sql("SELECT * FROM read_json('/etc/shadow')")

    def test_glob(self) -> None:
        with pytest.raises(ASTValidationError, match="blocked_function|unsafe_node"):
            validate_sql("SELECT * FROM glob('/home/*/.ssh/*')")

    def test_read_blob(self) -> None:
        with pytest.raises(ASTValidationError, match="blocked_function|unsafe_node"):
            validate_sql("SELECT * FROM read_blob('/etc/hosts')")


# ─────────────────────────────────────────────────────────────────────
# Multi-statement injection — these MUST fail
# ─────────────────────────────────────────────────────────────────────


class TestMultiStatement:
    """Ensure multi-statement injection is blocked."""

    def test_select_then_drop(self) -> None:
        with pytest.raises(ASTValidationError, match="multi_statement"):
            validate_sql("SELECT 1; DROP TABLE orders;")

    def test_select_then_delete(self) -> None:
        with pytest.raises(ASTValidationError, match="multi_statement"):
            validate_sql("SELECT 1; DELETE FROM customers WHERE 1=1;")

    def test_comment_obfuscated(self) -> None:
        with pytest.raises(ASTValidationError):
            validate_sql("SELECT/**/1;/**/DROP/**/TABLE/**/x")


# ─────────────────────────────────────────────────────────────────────
# DuckDB system commands — these MUST fail
# ─────────────────────────────────────────────────────────────────────


class TestBlockedSystemCommands:
    """Ensure DuckDB system/config commands are blocked."""

    def test_attach(self) -> None:
        with pytest.raises(ASTValidationError):
            validate_sql("ATTACH ':memory:' AS pwn")

    def test_install_extension(self) -> None:
        with pytest.raises(ASTValidationError):
            validate_sql("INSTALL httpfs")

    def test_load_extension(self) -> None:
        with pytest.raises(ASTValidationError):
            validate_sql("LOAD httpfs")

    def test_copy_to(self) -> None:
        with pytest.raises(ASTValidationError):
            validate_sql("COPY orders TO '/tmp/exfil.csv' (FORMAT CSV)")

    def test_export_database(self) -> None:
        with pytest.raises(ASTValidationError):
            validate_sql("EXPORT DATABASE '/tmp/dump'")

    def test_pragma(self) -> None:
        with pytest.raises(ASTValidationError):
            validate_sql("PRAGMA enable_progress_bar")

    def test_set_config(self) -> None:
        with pytest.raises(ASTValidationError):
            validate_sql("SET enable_external_access = true")


# ─────────────────────────────────────────────────────────────────────
# LIMIT injection
# ─────────────────────────────────────────────────────────────────────


class TestLimitInjection:
    """Ensure LIMIT is properly injected when missing."""

    def test_limit_injected_when_missing(self) -> None:
        result = validate_sql("SELECT * FROM orders", max_rows=100)
        assert "LIMIT" in result
        assert "100" in result

    def test_existing_small_limit_preserved(self) -> None:
        result = validate_sql("SELECT * FROM orders LIMIT 10", max_rows=5001)
        assert "LIMIT" in result
        # The existing limit of 10 should be preserved (it's smaller than max)
        assert "10" in result

    def test_existing_large_limit_capped(self) -> None:
        result = validate_sql("SELECT * FROM orders LIMIT 999999", max_rows=5001)
        assert "LIMIT" in result
        assert "5001" in result


# ─────────────────────────────────────────────────────────────────────
# Edge cases
# ─────────────────────────────────────────────────────────────────────


class TestEdgeCases:
    """Edge cases and boundary conditions."""

    def test_empty_query(self) -> None:
        with pytest.raises(ASTValidationError, match="empty"):
            validate_sql("")

    def test_whitespace_only(self) -> None:
        with pytest.raises(ASTValidationError, match="empty"):
            validate_sql("   \n\t  ")

    def test_trailing_semicolon_ok(self) -> None:
        """A trailing semicolon on a single statement should be fine."""
        result = validate_sql("SELECT * FROM orders LIMIT 10;")
        assert "orders" in result

    def test_deeply_nested_subquery(self) -> None:
        """Deeply nested queries should still work (within limits)."""
        sql = "SELECT * FROM (SELECT * FROM (SELECT * FROM orders LIMIT 5) t1) t2"
        result = validate_sql(sql)
        assert "orders" in result


# ─────────────────────────────────────────────────────────────────────
# Batch validation
# ─────────────────────────────────────────────────────────────────────


class TestBatchValidation:
    """Test batch validation for red-team reporting."""

    def test_mixed_batch(self) -> None:
        queries = [
            "SELECT * FROM orders LIMIT 10",  # Valid
            "DROP TABLE orders",  # Invalid
            "SELECT COUNT(*) FROM customers",  # Valid
            "DELETE FROM orders",  # Invalid
        ]
        results = validate_batch(queries)

        assert len(results) == 4
        assert results[0].is_valid is True
        assert results[1].is_valid is False
        assert results[2].is_valid is True
        assert results[3].is_valid is False
