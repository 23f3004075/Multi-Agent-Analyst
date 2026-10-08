from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.api.server import app
from src.auth.store import (
    authenticate_user,
    clear_user_history,
    get_user_by_token,
    get_user_history,
    init_user_store,
    logout_user,
    register_user,
    save_user_history,
)


import uuid

@pytest.fixture(autouse=True)
def setup_auth_db():
    init_user_store()


def test_auth_store_registration_and_login():
    test_email = f"test_{uuid.uuid4().hex[:8]}@enterprise.ai"
    reg = register_user(
        name="Test Analyst",
        email=test_email,
        password="securepassword123",
    )
    assert reg["token"]
    assert reg["user"]["email"] == test_email
    assert reg["user"]["display_name"] == "Test Analyst"

    user = get_user_by_token(reg["token"])
    assert user is not None
    assert user["email"] == test_email

    auth_by_email = authenticate_user(test_email, "securepassword123")
    assert auth_by_email["token"]
    assert auth_by_email["user"]["email"] == test_email

    auth_by_username = authenticate_user(reg["user"]["username"], "securepassword123")
    assert auth_by_username["token"]

    with pytest.raises(ValueError):
        authenticate_user(test_email, "wrongpassword")

    logged_out = logout_user(auth_by_email["token"])
    assert logged_out is True
    assert get_user_by_token(auth_by_email["token"]) is None


def test_auth_history_lifecycle():
    user = authenticate_user("admin@enterprise.ai", "admin123")["user"]
    save_user_history(
        user_id=user["id"],
        session_id="test_sess_42",
        query="SELECT COUNT(*) FROM olist_orders;",
        status="SUCCESS",
        route_decision="TIER_1_SLM",
        model_used="test-model",
        generated_sql="SELECT COUNT(*) FROM olist_orders;",
        summary="Found orders.",
        result_json="{}",
    )

    history = get_user_history(user["id"], session_id="test_sess_42")
    assert len(history) >= 1
    assert history[0]["query"] == "SELECT COUNT(*) FROM olist_orders;"

    clear_user_history(user["id"])
    cleared = get_user_history(user["id"], session_id="test_sess_42")
    assert len(cleared) == 0


def test_fastapi_auth_endpoints():
    client = TestClient(app)

    login_res = client.post(
        "/api/auth/login",
        json={"identifier": "admin@enterprise.ai", "password": "admin123"},
    )
    assert login_res.status_code == 200
    token = login_res.json()["token"]

    me_res = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me_res.status_code == 200
    assert me_res.json()["email"] == "admin@enterprise.ai"

    history_res = client.get(
        "/api/user/history",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert history_res.status_code == 200


def test_sql_extraction_with_reasoning_preamble():
    from src.agents.nodes.sql_generator import _extract_sql

    noisy_response = """\
1. SELECT statement (or WITH/CTE) - DuckDB SQL dialect - No DROP, DELETE...
The question asks for top 5 brands with highest avg rating.
There is no brand column, so we proxy by product category.

```sql
SELECT
    p.product_category_name AS brand,
    ROUND(AVG(r.review_score), 2) AS avg_rating
FROM order_reviews r
JOIN order_items oi ON r.order_id = oi.order_id
JOIN products p ON oi.product_id = p.product_id
GROUP BY p.product_category_name
ORDER BY avg_rating DESC
LIMIT 5;
```
Hope this helps!"""

    extracted = _extract_sql(noisy_response)
    assert "SELECT" in extracted
    assert "product_category_name" in extracted
    assert "DuckDB SQL dialect" not in extracted


def test_ast_validator_allows_data_type_param():
    from src.guardrails.sql_ast_checker import validate_sql

    sql = "SELECT CAST(price AS DECIMAL(10, 2)) AS formatted_price FROM order_items LIMIT 10;"
    result = validate_sql(sql)
    assert "DECIMAL(10, 2)" in result or "formatted_price" in result

