from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.guardrails.sql_ast_checker import ASTValidationError, validate_sql

PAYLOADS_PATH = Path(__file__).resolve().parent.parent / "src" / "security" / "red_team_payloads.json"

def load_payloads() -> list[dict]:
    with open(PAYLOADS_PATH) as f:
        return json.load(f)

PAYLOADS = load_payloads()
PAYLOAD_IDS = [f"{p['id']}_{p['category']}" for p in PAYLOADS]

@pytest.mark.parametrize("payload", PAYLOADS, ids=PAYLOAD_IDS)
def test_red_team_payload_blocked(payload: dict) -> None:
    sql = payload["payload"]
    payload_id = payload["id"]
    category = payload["category"]
    description = payload["description"]

    try:
        # This SHOULD raise ASTValidationError
        result = validate_sql(sql)

        # If we get here, the payload was NOT blocked — FAIL
        pytest.fail(
            f"🔴 SECURITY BYPASS: {payload_id} ({category})\n"
            f"   Description: {description}\n"
            f"   Payload: {sql}\n"
            f"   Validated as: {result}\n"
            f"   This payload should have been blocked!"
        )

    except ASTValidationError as e:
        # Expected — the payload was correctly blocked
        pass

    except Exception as e:
        # Any other exception (e.g., sqlglot ParseError) is also acceptable
        # as long as the payload didn't execute successfully
        pass

def test_red_team_summary() -> None:
    bypasses = []

    for payload in PAYLOADS:
        sql = payload["payload"]
        try:
            result = validate_sql(sql)
            bypasses.append({
                "id": payload["id"],
                "category": payload["category"],
                "payload": sql,
                "result": result,
            })
        except (ASTValidationError, Exception):
            pass  # Correctly blocked

    if bypasses:
        bypass_report = "\n".join(
            f"  - {b['id']} ({b['category']}): {b['payload'][:80]}"
            for b in bypasses
        )
        pytest.fail(
            f"🔴 {len(bypasses)} of {len(PAYLOADS)} red-team payloads BYPASSED validation:\n"
            f"{bypass_report}\n\n"
            f"Security model has critical gaps. Fix sql_ast_checker.py before proceeding."
        )

    # All blocked
    print(f"\n✅ Red-team security: 0 of {len(PAYLOADS)} payloads bypassed (100% block rate)")

def test_red_team_coverage() -> None:
    categories = {p["category"] for p in PAYLOADS}

    required_categories = {
        "filesystem_read",
        "multi_statement",
        "data_mutation",
        "schema_mutation",
        "extension_load",
        "data_exfil",
        "config_override",
    }

    missing = required_categories - categories
    assert not missing, (
        f"Red-team suite is missing attack categories: {missing}. "
        f"Add payloads for these categories to red_team_payloads.json"
    )

    # Ensure minimum payload count
    assert len(PAYLOADS) >= 50, (
        f"Red-team suite has only {len(PAYLOADS)} payloads. "
        f"Minimum is 50 for adequate coverage."
    )
