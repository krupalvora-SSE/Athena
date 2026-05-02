"""
Batch 1: 12 skills proposed in proposed_skills.md.
Idempotent — skips skills whose skill_id already exists.

Coverage:
  Navigation (Batch A — fixes observed log gaps):
    find-profit-loss, find-balance-sheet, find-stock-balance-report,
    find-general-ledger, find-trial-balance, find-open-pos, find-open-sos
  Workflow (Batch B):
    sales-cycle, manufacturing-cycle, sales-return, purchase-return,
    bank-reconciliation
"""
import db
from datetime import datetime, timezone

now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")
admin = "Administrator"

skills = [
    # ----- Batch A: Navigation skills -----
    {
        "skill_id": "find-profit-loss",
        "title": "Where to find Profit & Loss",
        "intent_pattern": r"profit\s*(and|&)\s*loss|p\s*[&]\s*l\b|profit\s+for\s+(fy|q[1-4]|\d{4})|what\s+is\s+(my\s+|the\s+)?profit",
        "description": "Deep-link to the standard ERPNext Profit and Loss Statement query report.",
        "content_md": (
            "**Profit & Loss Statement**\n\n"
            "Open the standard report at `/app/query-report/Profit and Loss Statement`.\n\n"
            "- Filters: **Company**, **From Date**, **To Date**, **Periodicity** (Monthly/Quarterly/Yearly), **Finance Book**.\n"
            "- For **FY 25-26** set From Date = `2025-04-01`, To Date = `2026-03-31`.\n"
            "- Click any row to drill into the underlying General Ledger entries.\n\n"
            "_Athena cannot compute P&L directly — it depends on chart of accounts, "
            "accounting periods, and fiscal-year setup that only the report engine handles correctly._"
        ),
    },
    {
        "skill_id": "find-balance-sheet",
        "title": "Where to find Balance Sheet",
        "intent_pattern": r"\bbalance\s+sheet\b",
        "description": "Deep-link to the standard ERPNext Balance Sheet query report.",
        "content_md": (
            "**Balance Sheet**\n\n"
            "Open at `/app/query-report/Balance Sheet`.\n\n"
            "- Filters: **Company**, **As On Date**, **Finance Book**, **Periodicity**.\n"
            "- Shows Assets, Liabilities, Equity grouped by parent account.\n"
            "- Click any account row to drill into the General Ledger."
        ),
    },
    {
        "skill_id": "find-stock-balance-report",
        "title": "Where to find Stock Balance",
        "intent_pattern": r"(where|find|view|see).{0,20}stock\s+balance|stock\s+balance\s+report|warehouse[- ]wise\s+stock",
        "description": "Deep-link to the Stock Balance report and Bin list, distinct from item-level stock lookup.",
        "content_md": (
            "**Stock Balance — two ways to view it**\n\n"
            "- **Stock Balance report** — `/app/query-report/Stock Balance`. Filters by Company, Warehouse, Item, From/To Date. Best for a point-in-time snapshot.\n"
            "- **Bin list** — `/app/bin`. Live `actual_qty`, `reserved_qty`, `projected_qty` per item × warehouse.\n\n"
            "_For a single item across all warehouses, ask Athena directly: e.g. `Stock of SLR-100W`._"
        ),
    },
    {
        "skill_id": "find-general-ledger",
        "title": "Where to find General Ledger",
        "intent_pattern": r"\bgeneral\s+ledger\b|\bgl\s+entries?\b|\bgl\s+report\b",
        "description": "Deep-link to the General Ledger query report with filter hints.",
        "content_md": (
            "**General Ledger**\n\n"
            "Open at `/app/query-report/General Ledger`.\n\n"
            "- Filters: **Company**, **From/To Date**, **Account**, **Voucher Type**, **Party Type**, **Cost Center**, **Project**.\n"
            "- To trace one document's accounting impact, set **Voucher No.** to the document name (e.g. `SI-2025-00123`).\n"
            "- Group By **Account** for an account-wise statement; **Voucher** for a transaction list."
        ),
    },
    {
        "skill_id": "find-trial-balance",
        "title": "Where to find Trial Balance",
        "intent_pattern": r"\btrial\s+balance\b",
        "description": "Deep-link to the Trial Balance report (account-wise and party-wise variants).",
        "content_md": (
            "**Trial Balance**\n\n"
            "Two variants of the report:\n\n"
            "- **Trial Balance** — `/app/query-report/Trial Balance` — opening, debit, credit, closing per account.\n"
            "- **Trial Balance for Party** — `/app/query-report/Trial Balance for Party` — same, grouped by Customer/Supplier.\n\n"
            "Filters: **Company**, **Fiscal Year**, **From/To Date**, **Show unclosed Fiscal Year P&L**."
        ),
    },
    {
        "skill_id": "find-open-pos",
        "title": "Find open Purchase Orders",
        "intent_pattern": r"(open|pending|outstanding|to\s+receive|unfulfilled).{0,20}purchase\s+orders?|\bopen\s+pos?\b",
        "description": "Deep-link to filtered Purchase Order list views by status.",
        "content_md": (
            "**Open Purchase Orders**\n\n"
            "Pre-filtered list views:\n\n"
            "- **Pending receipt + bill:** `/app/purchase-order?status=To Receive and Bill`\n"
            "- **Pending bill only:** `/app/purchase-order?status=To Bill`\n"
            "- **Pending receipt only:** `/app/purchase-order?status=To Receive`\n\n"
            "_The status field is updated automatically as Purchase Receipt / Purchase Invoice documents are submitted against the PO._"
        ),
    },
    {
        "skill_id": "find-open-sos",
        "title": "Find open Sales Orders",
        "intent_pattern": r"(open|pending|outstanding|to\s+deliver|unfulfilled).{0,20}sales\s+orders?|\bopen\s+sos?\b",
        "description": "Deep-link to filtered Sales Order list views by status.",
        "content_md": (
            "**Open Sales Orders**\n\n"
            "Pre-filtered list views:\n\n"
            "- **Pending delivery + bill:** `/app/sales-order?status=To Deliver and Bill`\n"
            "- **Pending bill only:** `/app/sales-order?status=To Bill`\n"
            "- **Pending delivery only:** `/app/sales-order?status=To Deliver`\n\n"
            "_The status field is updated as Delivery Note / Sales Invoice documents are submitted against the SO._"
        ),
    },
    # ----- Batch B: Workflow / process skills -----
    {
        "skill_id": "sales-cycle",
        "title": "Sales cycle (order-to-cash)",
        "intent_pattern": r"\bsales\s+(cycle|flow|process)\b|\border[- ]to[- ]cash\b|\bo2c\b",
        "description": "End-to-end O2C cycle from Quotation to Payment Entry.",
        "content_md": (
            "**Sales cycle (Order-to-Cash)**\n\n"
            "1. **Quotation** — `/app/quotation/new`. Optional, customer-facing.\n"
            "2. **Sales Order** (SO) — submit to commit.\n"
            "3. **Delivery Note** (DN) — picks stock, decreases inventory.\n"
            "4. **Sales Invoice** (SI) — book receivable. Can be from SO or DN.\n"
            "5. **Payment Entry** (PE) — settle the invoice.\n\n"
            "Each downstream doc has a **Get Items From** button to pull from the upstream doc."
        ),
    },
    {
        "skill_id": "manufacturing-cycle",
        "title": "Manufacturing cycle",
        "intent_pattern": r"\bmanufacturing\s+(cycle|flow|process)\b|\b(work\s+order|production)\s+(cycle|flow|process)\b",
        "description": "Manufacturing flow from BOM to finished goods.",
        "content_md": (
            "**Manufacturing cycle**\n\n"
            "1. **BOM** (Bill of Materials) — `/app/bom/new`. Defines item structure.\n"
            "2. **Work Order** — created from BOM, allocates raw materials.\n"
            "3. **Stock Entry — Material Transfer for Manufacture** — moves RM to WIP warehouse.\n"
            "4. **Stock Entry — Manufacture** — consumes RM, produces FG into target warehouse.\n"
            "5. *(optional)* **Quality Inspection** before FG is moved to stores.\n\n"
            "_Required setup:_ Manufacturing module enabled, default Work-in-Progress and Finished Goods warehouses on the Item or Item Group."
        ),
    },
    {
        "skill_id": "sales-return",
        "title": "Sales return / credit note",
        "intent_pattern": r"\bsales\s+return\b|credit\s+note\s+(against|of|for)|customer\s+return|return\s+against\s+(delivery\s+note|sales\s+invoice)",
        "description": "Return goods or credit a customer — DN return vs. credit-note SI.",
        "content_md": (
            "**Sales Return**\n\n"
            "Two paths depending on whether the goods were already invoiced:\n\n"
            "- **Return against Delivery Note** (goods only, not yet billed):\n"
            "  Open the original DN → **Create → Return**. Quantities are negative. "
            "Stock comes back to the source warehouse.\n\n"
            "- **Credit Note against Sales Invoice** (already billed):\n"
            "  Open the original SI → **Create → Return / Credit Note**. Reverses the receivable.\n\n"
            "Both submit as new documents linked to the original."
        ),
    },
    {
        "skill_id": "purchase-return",
        "title": "Purchase return / debit note",
        "intent_pattern": r"\bpurchase\s+return\b|debit\s+note\s+(against|of|for)|supplier\s+return|return\s+(goods?|items?)\s+to\s+supplier",
        "description": "Return goods or debit a supplier — GRN return vs. debit-note PI.",
        "content_md": (
            "**Purchase Return**\n\n"
            "- **Return against Purchase Receipt** (goods rejected, not yet billed):\n"
            "  Open the GRN → **Create → Return**. Stock leaves the warehouse.\n\n"
            "- **Debit Note against Purchase Invoice** (already booked):\n"
            "  Open the PI → **Create → Return / Debit Note**. Reverses the payable."
        ),
    },
    {
        "skill_id": "bank-reconciliation",
        "title": "Bank reconciliation",
        "intent_pattern": r"\bbank\s+reconciliation\b|(reconcile|match)\s+bank\s+statement|bank\s+statement\s+(import|upload)",
        "description": "Match imported bank transactions to ERPNext payment entries.",
        "content_md": (
            "**Bank Reconciliation**\n\n"
            "1. **Bank Statement Import** — `/app/bank-transaction`. Upload CSV/MT940.\n"
            "2. **Bank Reconciliation Tool** — `/app/bank-reconciliation-tool`. Match transactions against ERPNext Payment Entries / Journal Entries.\n"
            "3. Unmatched entries can use **Create Voucher** → produces a Payment Entry on the fly.\n"
            "4. Final state: every Bank Transaction row has a linked voucher."
        ),
    },
]

inserted, skipped = [], []
with db.db_cursor() as cur:
    for sk in skills:
        cur.execute("SELECT name FROM `tabAthena Skill` WHERE skill_id = %s", (sk["skill_id"],))
        if cur.fetchone():
            skipped.append(sk["skill_id"])
            continue
        cur.execute(
            """
            INSERT INTO `tabAthena Skill`
                (name, creation, modified, modified_by, owner, docstatus, idx,
                 skill_id, title, status, skill_type, linked_tool,
                 intent_pattern, description, content_md, use_count)
            VALUES
                (%s, %s, %s, %s, %s, 0, 0,
                 %s, %s, 'Published', 'Workflow', '',
                 %s, %s, %s, 0)
            """,
            (
                sk["skill_id"], now, now, admin, admin,
                sk["skill_id"], sk["title"],
                sk["intent_pattern"], sk["description"], sk["content_md"],
            ),
        )
        inserted.append(sk["skill_id"])

print(f"Inserted ({len(inserted)}):")
for s in inserted:
    print(f"  + {s}")
if skipped:
    print(f"\nSkipped ({len(skipped)}, already exist):")
    for s in skipped:
        print(f"  · {s}")

# Sanity: regex compilation check (Athena doesn't compile here but Krupal's
# validate() does in Frappe — this just surfaces obvious mistakes early).
import re as _re
print("\nRegex compile check:")
for sk in skills:
    try:
        _re.compile(sk["intent_pattern"])
        print(f"  OK    {sk['skill_id']}")
    except _re.error as e:
        print(f"  FAIL  {sk['skill_id']}: {e}")
