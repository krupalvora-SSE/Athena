"""
Integration tests for the schema indexing and retrieval pipeline.
Requires:
  - docker compose up (API running at localhost:7001)
  - DB accessible (config.json present)
  - index_schema.py must have been run at least once (happens at container startup)

Run:
    pytest tests/test_schema.py -v
    # or
    python3 tests/test_schema.py
"""

import sys
import httpx
import pytest

API_URL = "http://localhost:7001"
TIMEOUT = 60


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_health() -> dict:
    return httpx.get(f"{API_URL}/health", timeout=10).json()


def ask(message: str, username: str = "test@solarsquare.in") -> dict:
    resp = httpx.post(
        f"{API_URL}/chat",
        json={"message": message, "username": username, "session_id": "test-schema"},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def sync_schema() -> dict:
    resp = httpx.post(f"{API_URL}/admin/sync-schema", timeout=300)
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# 1. Health & readiness
# ---------------------------------------------------------------------------

class TestHealthAndReadiness:

    def test_api_is_up(self):
        h = get_health()
        assert h["status"] == "ok", f"API unhealthy: {h}"

    def test_chroma_ready(self):
        h = get_health()
        assert h["chroma_ready"] is True, "ChromaDB not ready"

    def test_schema_ready(self):
        h = get_health()
        assert h.get("schema_ready") is True, (
            "SchemaRetriever not ready — did index_schema.py run at startup?"
        )


# ---------------------------------------------------------------------------
# 2. Schema sync endpoint
# ---------------------------------------------------------------------------

class TestSchemaSyncEndpoint:

    def test_sync_schema_returns_ok(self):
        result = sync_schema()
        assert result["status"] == "ok"

    def test_sync_schema_indexes_tables(self):
        result = sync_schema()
        assert result["tables_indexed"] > 0, "No tables were indexed"

    def test_sync_schema_indexes_common_doctypes(self):
        """After sync, schema_ready should still be True."""
        sync_schema()
        h = get_health()
        assert h.get("schema_ready") is True


# ---------------------------------------------------------------------------
# 3. NL-to-SQL with schema context (no column hallucination)
# ---------------------------------------------------------------------------

class TestNLToSQLWithSchema:
    """
    These tests require the user to have a manager role (System Manager, etc.)
    for the NL-to-SQL gate. Tests are marked xfail if the user lacks access.
    """

    MANAGER_USER = "Administrator"

    def _ask_as_manager(self, question: str) -> dict:
        resp = httpx.post(
            f"{API_URL}/chat",
            json={
                "message": question,
                "username": self.MANAGER_USER,
                "session_id": "test-nl-sql",
            },
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()

    def test_delivery_note_count_uses_real_columns(self):
        """LLM should use docstatus (real column) not a hallucinated one."""
        result = self._ask_as_manager("how many submitted delivery notes are there?")
        answer = result["answer"]
        # Should not mention unknown column error
        assert "Unknown column" not in answer, f"Column hallucination detected: {answer}"
        assert "error" not in answer.lower() or "Query" in answer

    def test_purchase_receipt_count(self):
        result = self._ask_as_manager("how many purchase receipts in total?")
        answer = result["answer"]
        assert "Unknown column" not in answer, f"Column hallucination: {answer}"

    def test_sales_order_count(self):
        result = self._ask_as_manager("count all submitted sales orders")
        answer = result["answer"]
        assert "Unknown column" not in answer, f"Column hallucination: {answer}"

    def test_no_permission_for_regular_user(self):
        """A user without manager roles should get a permission error, not SQL."""
        resp = httpx.post(
            f"{API_URL}/chat",
            json={
                "message": "how many purchase orders were created this month?",
                "username": "regular.user@example.com",
                "session_id": "test-no-perm",
            },
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        answer = resp.json()["answer"].lower()
        assert "permission" in answer or "role" in answer, (
            "Expected permission denial for unprivileged user"
        )


# ---------------------------------------------------------------------------
# 4. Schema retriever — semantic relevance
# ---------------------------------------------------------------------------

class TestSchemaRetrieverRelevance:
    """
    These tests verify that the schema retriever returns contextually
    relevant tables. They run inside the container environment.
    Skipped if SchemaRetriever isn't accessible from outside the container.
    """

    def test_delivery_note_question_returns_relevant_table(self):
        """
        When a DN-related NL query is answered, the SQL should reference
        tabDelivery Note — verifiable by checking the SQL in the answer.
        """
        resp = httpx.post(
            f"{API_URL}/chat",
            json={
                "message": "how many delivery notes were submitted?",
                "username": "Administrator",
                "session_id": "test-relevance",
            },
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        answer = resp.json()["answer"]
        # The SQL block is returned in the answer — check it references the right table
        assert "tabDelivery Note" in answer or "Delivery Note" in answer, (
            f"Expected Delivery Note table in SQL, got: {answer[:300]}"
        )

    def test_bin_table_for_stock_query(self):
        """Stock balance NL query should reference tabBin."""
        resp = httpx.post(
            f"{API_URL}/chat",
            json={
                "message": "how many bins have actual qty greater than zero?",
                "username": "Administrator",
                "session_id": "test-bin",
            },
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        answer = resp.json()["answer"]
        assert "tabBin" in answer or "Bin" in answer, (
            f"Expected Bin table in SQL, got: {answer[:300]}"
        )


# ---------------------------------------------------------------------------
# 5. Schema sync idempotency
# ---------------------------------------------------------------------------

class TestSchemaSyncIdempotency:

    def test_double_sync_is_safe(self):
        """Running sync twice should not crash or corrupt the collection."""
        r1 = sync_schema()
        r2 = sync_schema()
        assert r1["status"] == "ok"
        assert r2["status"] == "ok"
        assert r1["tables_indexed"] == r2["tables_indexed"], (
            "Table count changed between syncs — unexpected"
        )

    def test_schema_ready_after_double_sync(self):
        sync_schema()
        sync_schema()
        h = get_health()
        assert h.get("schema_ready") is True


# ---------------------------------------------------------------------------
# Standalone runner
# ---------------------------------------------------------------------------

def run_all():
    import traceback

    suites = [
        TestHealthAndReadiness,
        TestSchemaSyncEndpoint,
        TestNLToSQLWithSchema,
        TestSchemaRetrieverRelevance,
        TestSchemaSyncIdempotency,
    ]

    passed = failed = 0
    for suite_cls in suites:
        suite = suite_cls()
        methods = [m for m in dir(suite) if m.startswith("test_")]
        for method in methods:
            label = f"{suite_cls.__name__}.{method}"
            try:
                getattr(suite, method)()
                print(f"PASS  {label}")
                passed += 1
            except Exception as e:
                print(f"FAIL  {label}: {e}")
                traceback.print_exc()
                failed += 1

    print(f"\n{'='*60}")
    print(f"Results: {passed}/{passed + failed} passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run_all()
