"""
Integration tests for the ERP Chatbot RAG pipeline.
Hits the live API at localhost:8001 — make sure docker compose is running first.

Run:
    python3 tests/test_rag.py
"""

import json
import sys
import httpx

API_URL = "http://localhost:7001"
TIMEOUT = 120  # seconds — LLM can be slow


def ask(question: str) -> dict:
    resp = httpx.post(
        f"{API_URL}/chat",
        json={"message": question},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Test cases: (question, expected_keywords, label)
# expected_keywords: ALL of these must appear in the answer (case-insensitive)
# ---------------------------------------------------------------------------
TEST_CASES = [
    # --- GRN ---
    (
        "What are the workflow states for a Purchase Receipt (GRN)?",
        ["reviewing", "complete", "canceled"],
        "GRN workflow states",
    ),
    (
        "When does a GRN skip PM Review and go directly to Complete?",
        ["physical", "custom_is_physical"],
        "GRN physical warehouse bypass",
    ),
    (
        "What attachments are required before saving a GRN?",
        ["bill", "lr"],
        "GRN attachment validation",
    ),
    # --- Purchase Order ---
    (
        "What are the workflow states for a Purchase Order?",
        ["pending", "reviewing", "finance"],
        "PO workflow states",
    ),
    (
        "When does a Purchase Order go through Finance Approval?",
        ["finance", "custom_send_to_finance_approval"],
        "PO finance approval gate",
    ),
    # --- Sales Order ---
    (
        "What happens if you try to create a second Sales Order for the same customer?",
        ["duplicate", "customer"],
        "SO duplicate customer check",
    ),
    (
        "What are the workflow states for a Sales Order?",
        ["reviewing", "complete", "canceled"],
        "SO workflow states",
    ),
    # --- Delivery Note ---
    (
        "What naming series is used for a B2B Delivery Note?",
        ["B2B-.YY.-"],
        "DN naming series B2B",
    ),
    (
        "What status does a Vendor Serial Number get when a non-return Delivery Note is submitted to an external customer?",
        ["delivered to cx", "delivered"],
        "VSR status on DN submit to customer",
    ),
    # --- Picked Up (PKL) ---
    (
        "What project statuses block the Picked Up document from being validated?",
        ["awaiting design approval", "bom pending"],
        "PKL project status block",
    ),
]


def run_tests():
    # Health check first
    try:
        health = httpx.get(f"{API_URL}/health", timeout=10).json()
        if not health.get("chroma_ready"):
            print("FAIL  /health — chroma_ready is False. Is the API running?")
            sys.exit(1)
        print(f"OK    /health — {health}\n")
    except Exception as e:
        print(f"FAIL  Cannot reach API at {API_URL}: {e}")
        sys.exit(1)

    passed = 0
    failed = 0

    for question, expected_keywords, label in TEST_CASES:
        try:
            result = ask(question)
            answer = result["answer"].lower()
            sources = [s.split("/")[-1] for s in result["sources"]]

            missing = [kw for kw in expected_keywords if kw.lower() not in answer]

            if not missing:
                print(f"PASS  [{label}]")
                print(f"      sources: {sources}")
                passed += 1
            else:
                print(f"FAIL  [{label}]")
                print(f"      missing keywords: {missing}")
                print(f"      answer: {result['answer'][:200]}")
                print(f"      sources: {sources}")
                failed += 1
        except Exception as e:
            print(f"ERROR [{label}]: {e}")
            failed += 1

        print()

    total = passed + failed
    print(f"{'='*50}")
    print(f"Results: {passed}/{total} passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run_tests()
