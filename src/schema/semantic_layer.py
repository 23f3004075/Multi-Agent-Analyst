from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MetricDefinition:
    name: str
    display_name: str
    sql_expression: str
    description: str
    tables_involved: list[str]
    aliases: list[str] = field(default_factory=list)


OLIST_METRICS: list[MetricDefinition] = [
    MetricDefinition(
        name="revenue",
        display_name="Revenue",
        sql_expression="SUM(oi.price)",
        description="Total revenue from product prices (excludes freight). "
                    "Join: order_items oi",
        tables_involved=["order_items"],
        aliases=["sales", "income", "total sales", "total revenue"],
    ),
    MetricDefinition(
        name="total_revenue_with_freight",
        display_name="Total Revenue (incl. Freight)",
        sql_expression="SUM(oi.price + oi.freight_value)",
        description="Total revenue including freight charges. "
                    "Join: order_items oi",
        tables_involved=["order_items"],
        aliases=["gross revenue", "total amount", "total value"],
    ),
    MetricDefinition(
        name="order_count",
        display_name="Order Count",
        sql_expression="COUNT(DISTINCT o.order_id)",
        description="Number of distinct orders. Join: orders o",
        tables_involved=["orders"],
        aliases=["number of orders", "total orders", "orders"],
    ),
    MetricDefinition(
        name="average_order_value",
        display_name="Average Order Value (AOV)",
        sql_expression="SUM(oi.price) / COUNT(DISTINCT o.order_id)",
        description="Average revenue per order. "
                    "Join: orders o, order_items oi ON o.order_id = oi.order_id",
        tables_involved=["orders", "order_items"],
        aliases=["aov", "average order", "avg order value", "average basket"],
    ),
    MetricDefinition(
        name="customer_count",
        display_name="Customer Count",
        sql_expression="COUNT(DISTINCT c.customer_unique_id)",
        description="Number of unique customers (by customer_unique_id, "
                    "not customer_id which is per-order). Join: customers c",
        tables_involved=["customers"],
        aliases=["number of customers", "total customers", "unique customers"],
    ),
    MetricDefinition(
        name="average_review_score",
        display_name="Average Review Score",
        sql_expression="AVG(r.review_score)",
        description="Average customer review rating (1-5 scale). "
                    "Join: order_reviews r",
        tables_involved=["order_reviews"],
        aliases=["rating", "average rating", "review rating", "satisfaction"],
    ),
    MetricDefinition(
        name="review_count",
        display_name="Review Count",
        sql_expression="COUNT(DISTINCT r.review_id)",
        description="Total count of customer reviews. "
                    "For product reviews: products p JOIN order_items oi ON p.product_id = oi.product_id JOIN order_reviews r ON oi.order_id = r.order_id",
        tables_involved=["order_reviews", "order_items", "products"],
        aliases=[
            "review count", "reviews count", "reviews_count", "number of reviews",
            "total reviews", "review volume", "most reviewed", "product reviews"
        ],
    ),
    MetricDefinition(
        name="delivery_time_days",
        display_name="Delivery Time (Days)",
        sql_expression=(
            "DATE_DIFF('day', o.order_purchase_timestamp, "
            "o.order_delivered_customer_date)"
        ),
        description="Days between purchase and delivery. "
                    "Only for delivered orders. Join: orders o",
        tables_involved=["orders"],
        aliases=[
            "delivery time", "shipping time", "time to deliver",
            "delivery duration",
        ],
    ),
    MetricDefinition(
        name="freight_ratio",
        display_name="Freight Ratio",
        sql_expression="SUM(oi.freight_value) / SUM(oi.price)",
        description="Freight cost as a percentage of product price. "
                    "Join: order_items oi",
        tables_involved=["order_items"],
        aliases=["shipping ratio", "freight percentage", "shipping cost ratio"],
    ),
    MetricDefinition(
        name="seller_count",
        display_name="Active Sellers",
        sql_expression="COUNT(DISTINCT s.seller_id)",
        description="Number of distinct sellers. Join: sellers s",
        tables_involved=["sellers"],
        aliases=["number of sellers", "total sellers", "vendors"],
    ),
    MetricDefinition(
        name="items_per_order",
        display_name="Items per Order",
        sql_expression="COUNT(oi.order_item_id) * 1.0 / COUNT(DISTINCT oi.order_id)",
        description="Average number of items per order. Join: order_items oi",
        tables_involved=["order_items"],
        aliases=["basket size", "average items", "items count per order"],
    ),
    MetricDefinition(
        name="repeat_customer_rate",
        display_name="Repeat Customer Rate",
        sql_expression=(
            "COUNT(DISTINCT CASE WHEN order_count > 1 THEN customer_unique_id END) "
            "* 100.0 / COUNT(DISTINCT customer_unique_id)"
        ),
        description="Percentage of customers who placed more than 1 order. "
                    "Requires subquery: first compute order_count per "
                    "customer_unique_id. Join: orders o, customers c",
        tables_involved=["orders", "customers"],
        aliases=["retention rate", "returning customers", "repeat rate"],
    ),
    MetricDefinition(
        name="cancellation_rate",
        display_name="Cancellation Rate",
        sql_expression=(
            "COUNT(CASE WHEN o.order_status = 'canceled' THEN 1 END) * 100.0 "
            "/ COUNT(*)"
        ),
        description="Percentage of orders that were canceled. Join: orders o",
        tables_involved=["orders"],
        aliases=["cancel rate", "cancelled orders percentage"],
    ),
]


class SemanticLayer:
    def __init__(self, metrics: list[MetricDefinition] | None = None) -> None:
        self._metrics = metrics or OLIST_METRICS
        self._alias_index = self._build_alias_index()

    def _build_alias_index(self) -> dict[str, MetricDefinition]:
        index: dict[str, MetricDefinition] = {}
        for metric in self._metrics:
            index[metric.name.lower()] = metric
            index[metric.display_name.lower()] = metric
            for alias in metric.aliases:
                index[alias.lower()] = metric
        return index

    def find_relevant_metrics(self, query: str) -> list[MetricDefinition]:
        query_lower = query.lower()
        found: list[MetricDefinition] = []
        seen_names: set[str] = set()

        for alias, metric in self._alias_index.items():
            if alias in query_lower and metric.name not in seen_names:
                found.append(metric)
                seen_names.add(metric.name)

        return found

    def get_metric_context(self, query: str) -> str:
        metrics = self.find_relevant_metrics(query)

        if not metrics:
            return ""

        lines = [
            "## Business Metric Definitions",
            "Use these EXACT definitions when computing metrics:",
            "",
        ]
        for m in metrics:
            lines.append(f"- **{m.display_name}**: `{m.sql_expression}`")
            lines.append(f"  {m.description}")
            lines.append("")

        return "\n".join(lines)

    def get_full_glossary(self) -> str:
        lines = [
            "## Business Metric Glossary (Olist E-Commerce)",
            "Always use these definitions for business terms:",
            "",
        ]
        for m in self._metrics:
            aliases = ", ".join(m.aliases) if m.aliases else "none"
            lines.append(f"### {m.display_name}")
            lines.append(f"- SQL: `{m.sql_expression}`")
            lines.append(f"- Description: {m.description}")
            lines.append(f"- Tables: {', '.join(m.tables_involved)}")
            lines.append(f"- Also known as: {aliases}")
            lines.append("")

        return "\n".join(lines)

    def get_tables_for_metrics(self, query: str) -> list[str]:
        metrics = self.find_relevant_metrics(query)
        tables: set[str] = set()
        for m in metrics:
            tables.update(m.tables_involved)
        return sorted(tables)
