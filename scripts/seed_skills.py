"""One-shot seeder for sample Athena Skills. Idempotent by skill_id."""
import db
from datetime import datetime, timezone

now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")
admin = "Administrator"

skills = [
    {
        "skill_id": "material-transfer-howto",
        "title": "How to do a Material Transfer",
        "intent_pattern": r"\bmaterial\s+transfer\b.*\b(how|steps?|process|workflow|procedure)\b|\b(how|steps?|process)\b.*\bmaterial\s+transfer\b",
        "description": "Step-by-step procedure for stock material transfer between warehouses.",
        "content_md": (
            "**Material Transfer (Stock Entry -> Material Transfer)**\n\n"
            "1. Open **Stock Entry -> New** (`/app/stock-entry/new`).\n"
            "2. Set **Stock Entry Type** to *Material Transfer*.\n"
            "3. Pick **Source Warehouse** and **Target Warehouse**.\n"
            "4. Add items + quantities in the Items table.\n"
            "5. Save -> Submit.\n\n"
            "_Tip: for inter-company moves use **Stock Entry Type = Material Transfer for Manufacture** instead._"
        ),
    },
    {
        "skill_id": "data-import-howto",
        "title": "How to import data",
        "intent_pattern": r"\bdata\s+import\b|\bimport\s+(data|records|csv|excel)\b|\bbulk\s+(upload|import)\b",
        "description": "Guide to ERPNext Data Import tool.",
        "content_md": (
            "**Data Import**\n\n"
            "1. Go to **Data Import -> New** (`/app/data-import/new`).\n"
            "2. Select the **Document Type** to import.\n"
            "3. Choose **Import Type** -- *Insert New* or *Update Existing*.\n"
            "4. **Download Template** (matches selected doctype's fields).\n"
            "5. Fill the template, upload it.\n"
            "6. Click **Save**, then **Start Import**.\n\n"
            "_For updates you must include the `name` (ID) column._"
        ),
    },
    {
        "skill_id": "purchase-order-cycle",
        "title": "Purchase Order -- full procurement cycle",
        "intent_pattern": r"\bpurchase\s+order\b.*\b(cycle|flow|process|end\s*[- ]?\s*to\s*[- ]?\s*end)\b|\bprocurement\s+(cycle|flow|process)\b",
        "description": "End-to-end PO lifecycle from MR to PI payment.",
        "content_md": (
            "**Procurement cycle**\n\n"
            "1. **Material Request** (MR) -- internal request, optional.\n"
            "2. **Request for Quotation** (RFQ) -> **Supplier Quotation** (SQ).\n"
            "3. **Purchase Order** (PO) -- submit to commit.\n"
            "4. **Purchase Receipt** (GRN) -- receive goods, updates stock.\n"
            "5. **Purchase Invoice** (PI) -- book payable, optionally from GRN.\n"
            "6. **Payment Entry** (PE) -- settle the invoice.\n\n"
            "Each step links back via the *Get Items From* button."
        ),
    },
    {
        "skill_id": "stock-reconciliation-howto",
        "title": "How to do a Stock Reconciliation",
        "intent_pattern": r"\bstock\s+reconciliation\b|\bopening\s+stock\b.*\b(set|enter|update)\b|\bphysical\s+stock\s+count\b",
        "description": "Adjust on-hand quantities/valuation to match physical count.",
        "content_md": (
            "**Stock Reconciliation**\n\n"
            "1. Open **Stock Reconciliation -> New** (`/app/stock-reconciliation/new`).\n"
            "2. Set **Purpose**: *Stock Reconciliation* (regular) or *Opening Stock*.\n"
            "3. Click **Get Items** -> filter by Warehouse/Item Group -> fetch.\n"
            "4. Edit **Quantity** and/or **Valuation Rate** rows.\n"
            "5. Save -> Submit. The difference is posted as a stock ledger entry.\n\n"
            "_Use **Opening Stock** purpose only on go-live, not for routine corrections._"
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

print("Inserted:", inserted)
print("Skipped (already exist):", skipped)

with db.db_cursor() as cur:
    cur.execute(
        "SELECT skill_id, status, skill_type, LEFT(intent_pattern, 60) AS pattern "
        "FROM `tabAthena Skill` ORDER BY skill_id"
    )
    print("\nCurrent rows:")
    for r in cur.fetchall():
        print(f"  - {r['skill_id']:<35} [{r['status']:<10}] {r['skill_type']:<10} {r['pattern']}")
