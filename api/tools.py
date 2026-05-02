"""
Phase 2 — ERPNext live query tools.

Routing strategy (in order):
  1. Regex router  — fast, zero latency, handles known patterns
  2. LLM classifier — fallback when regex misses, uses Ollama JSON mode
  3. RAG            — returned None from both above → docs pipeline

Intent taxonomy:
  user_roles    — what roles do I/user X have?
  role_perms    — what permissions does role X have? (optionally filtered by doctype)
  doctype_roles — which roles have access to doctype X?
  access_check  — can user X perform action on doctype Y?
  tasks         — open tasks / todos for the user
  stock         — inventory balance for item X
  rag           — general ERP docs question
"""

import re
import json
import os
import logging
import httpx

logger = logging.getLogger(__name__)

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434")
OLLAMA_MODEL    = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")

# ---------------------------------------------------------------------------
# Doctype alias map
# ---------------------------------------------------------------------------

DOCTYPE_ALIASES: dict[str, str] = {
    "DN":  "Delivery Note",
    "SO":  "Sales Order",
    "PO":  "Purchase Order",
    "PI":  "Purchase Invoice",
    "SI":  "Sales Invoice",
    "GRN": "Purchase Receipt",
    "PR":  "Purchase Receipt",
    "MR":  "Material Request",
    "STE": "Stock Entry",
    "SE":  "Stock Entry",
    "WO":  "Work Order",
    "BOM": "BOM",
    "JE":  "Journal Entry",
    "PE":  "Payment Entry",
    "PKL": "Pick List",
    "SR":  "Stock Reconciliation",
    "RFQ": "Request for Quotation",
    "SQ":  "Supplier Quotation",
    "SCO": "Subcontracting Order",
}

# Title-case phrase aliases — match free-text doctype mentions before falling back
# to the regex extractor. Lower-cased keys for case-insensitive lookup.
_PHRASE_ALIASES: dict[str, str] = {
    "purchase order": "Purchase Order",
    "sales order": "Sales Order",
    "delivery note": "Delivery Note",
    "purchase receipt": "Purchase Receipt",
    "purchase invoice": "Purchase Invoice",
    "sales invoice": "Sales Invoice",
    "material request": "Material Request",
    "stock entry": "Stock Entry",
    "stock reconciliation": "Stock Reconciliation",
    "journal entry": "Journal Entry",
    "payment entry": "Payment Entry",
    "work order": "Work Order",
    "pick list": "Pick List",
    "subcontracting order": "Subcontracting Order",
    "request for quotation": "Request for Quotation",
    "supplier quotation": "Supplier Quotation",
    "blanket order": "Blanket Order",
    "access request": "Access Request",
    "warehouse": "Warehouse",
    "item": "Item",
    "supplier": "Supplier",
    "customer": "Customer",
    "user": "User",
    "role": "Role",
    "bom": "BOM",
    "grn": "Purchase Receipt",
    "po": "Purchase Order",
    "so": "Sales Order",
    "dn": "Delivery Note",
}


def resolve_doctype(token: str) -> str:
    """Resolve a token (abbr/phrase/raw) to the canonical Frappe DocType name."""
    raw = token.strip()
    upper = raw.upper()
    if upper in DOCTYPE_ALIASES:
        return DOCTYPE_ALIASES[upper]
    lower = raw.lower()
    if lower in _PHRASE_ALIASES:
        return _PHRASE_ALIASES[lower]
    return raw


def _slugify_doctype(doctype: str) -> str:
    """ERPNext URL slug for a doctype: 'Purchase Order' → 'purchase-order'."""
    return doctype.strip().lower().replace(" ", "-")


def _extract_doctype_anywhere(text: str) -> str | None:
    """
    Best-effort doctype extraction from free text.
    Tries phrase aliases first (handles 'workflow of Purchase Order'), then abbrs.
    """
    lower = text.lower()
    # Sort longest-first so 'purchase order' wins over 'purchase'.
    for phrase in sorted(_PHRASE_ALIASES, key=len, reverse=True):
        if re.search(rf"\b{re.escape(phrase)}\b", lower):
            return _PHRASE_ALIASES[phrase]
    for abbr, canonical in DOCTYPE_ALIASES.items():
        if re.search(rf"\b{re.escape(abbr)}\b", text):
            return canonical
    return None


# ---------------------------------------------------------------------------
# Regex intent patterns
# ---------------------------------------------------------------------------

_ROLE_PERMS_RE = re.compile(
    r"(permissions?\s+(for|of|does|that)\s+role"
    r"|role\s*[-–:]\s*\S"
    r"|what\s+(can|permissions?).*(role)"
    r"|\brole\b.*(permissions?|access|can do|has\s+access))",
    re.I,
)
_DOCTYPE_ROLES_RE = re.compile(
    r"(for\s+(doctype\s+)?[A-Za-z].*what\s+roles?"
    r"|\bwho\s+can\s+(access|view|edit|submit|create)"
    r"|\baccess\s+(to\s+)?(doctype\s+)?\S"
    r"|what\s+roles?.*(access|permission).*(to\s+)?\S"
    r"|\b(BOM|DN|SO|PO|PI|SI|GRN|MR|WO|JE|PE|PKL|RFQ)\b.*(roles?|access|permission)"
    r"|(roles?|access).*(on|for|in|to)\s+\b[A-Z])",
    re.I,
)
_USER_ROLES_RE = re.compile(
    r"(my\s+roles?"
    r"|what\s+roles?\s+(do\s+i|i\s+have|does\s+\S+\s+have)"
    r"|roles?\s+(i\s+have|for\s+user|assigned\s+to\s+(me|user))"
    r"|what\s+all\s+roles?\s+(do\s+i|i\s+have)"
    r"|\bgive\s+(me\s+)?list\s+of\s+my\s+roles?"
    r"|\broles?\s+(for|of)\s+user\s+\S+@)",
    re.I,
)
_ACCESS_CHECK_RE = re.compile(
    r"(can\s+(i|user\s+\S+)\s+(access|view|edit|create|delete|submit)"
    r"|\bdo\s+i\s+have\s+(access|permission)\b)",
    re.I,
)
_TASK_RE  = re.compile(r"\b(my\s+(open\s+)?tasks?|open\s+tasks?|todos?|assigned\s+to\s+me)\b", re.I)
_STOCK_RE = re.compile(r"\b(stock|balance|qty|quantity|bin|warehouse|inventory)\b", re.I)
# Bug fix: added transfer/move/shift to guard against process questions hitting stock handler
_PROCESS_INTENT_RE = re.compile(
    r"\b(process|how\s+to|how\s+do|steps?\s+to|procedure|way\s+to|method\s+to"
    r"|what\s+is\s+the\s+step|next\s+step|transfer|move|shift|create|make)\b",
    re.I,
)
# Bug fix: added lowercase "role <name>" pattern to catch "list of users having access to role system manager"
_USERS_WITH_ROLE_RE = re.compile(
    r"(users?\s+(with|having|assigned|who\s+have)\s+(the\s+)?role"
    r"|list\s+(of\s+)?users?\s+(with|for|having)(\s+access\s+to)?\s+role"
    r"|who\s+(has|have|is\s+assigned)\s+(the\s+)?role"
    r"|\bhaving\s+access\s+to\s+role\b"
    r"|\baccess\s+to\s+role\b)",
    re.I,
)

# Detects references back to prior context ("that role", "it", "those")
_REFERS_BACK_RE = re.compile(r"\b(that|those|the same|it|its|their|this)\b", re.I)

# Identity questions — answered directly without hitting the LLM
# Bug fix: added hey/hi/hello athena so greetings don't fall to wrong handlers
_IDENTITY_RE = re.compile(
    r"\b(what\s+is\s+your\s+name"
    r"|who\s+are\s+you"
    r"|what\s+are\s+you\s+called"
    r"|who\s+(made|created|built|developed)\s+you"
    r"|introduce\s+yourself"
    r"|your\s+name"
    r"|hey\s+athena|hi\s+athena|hello\s+athena"
    r"|hey\s+there|greetings)\b",
    re.I,
)

# Bare greetings without "athena" — should introduce itself
_GREETING_RE = re.compile(
    r"^\s*(hi|hello|hey|good\s+(morning|afternoon|evening)|howdy|sup)\s*[!.\?]?\s*$",
    re.I,
)

# "what can you do", "what else can you help me with", "help me", etc.
_CAPABILITIES_RE = re.compile(
    r"(what\s+(can|else)\s+(you|athena)\s+(do|help)"
    r"|what\s+all\s+(can\s+you|you\s+can)"
    r"|how\s+can\s+you\s+help"
    r"|your\s+capabilities"
    r"|what\s+are\s+your\s+(features|capabilities|functions)"
    r"|help\s+me\s+with\s+(this|erp|erpnext))",
    re.I,
)

# "what is my username", "my email", "who am i"
_USERNAME_RE = re.compile(
    r"(what\s+is\s+my\s+(username|email|user\s*name|user\s*id)"
    r"|who\s+am\s+i"
    r"|\bmy\s+(username|email|user\s*name|user\s*id)\b)",
    re.I,
)

# "is user X enabled/active/disabled"
_USER_STATUS_RE = re.compile(
    r"\b(is\s+user\s+\S+\s+(enabled|active|disabled|blocked)"
    r"|user\s+\S+\s+(status|enabled|active|disabled))\b",
    re.I,
)

# Document detail lookup: "show me SO-2024-00123", "details of PO-2024-001"
_DOC_LOOKUP_RE = re.compile(
    r"(show\s+(me\s+)?|details?\s+(of\s+|for\s+)?|open\s+|fetch\s+|get\s+|what\s+is\s+)"
    r"([A-Z][A-Z0-9]{0,4}-\d{4}-\d{3,6})",
    re.I,
)
# Also catch bare docnames like "PO-2024-00123" anywhere in the message
_DOCNAME_RE = re.compile(r"\b([A-Z][A-Z0-9]{0,4}-\d{4}-\d{3,6})\b")

# Pending approvals: "pending my approval", "what needs approval", etc.
_PENDING_APPROVALS_RE = re.compile(
    r"(pending\s+(my\s+)?approval"
    r"|what\s+(needs|requires)\s+(my\s+)?approval"
    r"|documents?\s+(to|for)\s+approv"
    r"|approval\s+queue"
    r"|waiting\s+for\s+my\s+approval"
    r"|\bmy\s+approvals?\b)",
    re.I,
)

# Workflow questions: "workflow of PO", "states for Purchase Order", "approval flow for GRN"
_WORKFLOW_RE = re.compile(
    r"(\bworkflow\b|\bworkflow\s+state\b|\bworkflow\s+stages?\b"
    r"|\bapproval\s+(flow|process|chain|stages?|states?)\b"
    r"|\b(states|stages|transitions)\s+(of|for|in)\s+\S"
    r"|\blist\s+(of\s+)?(all\s+)?workflows?\b"
    r"|\bwhat\s+workflows?\s+exist\b"
    r"|\bwho\s+can\s+approve\b)",
    re.I,
)

# "How to / give me a link / take me there" — must fire BEFORE NL→SQL
# so phrases like "how to do data import" don't get translated to SQL.
_HOWTO_RE = re.compile(
    r"(\bhow\s+(to|do|can)\s+(i|we|you)?\s*\w"
    r"|\bhow\s+to\s+\w"
    r"|\bsteps?\s+to\s+\w"
    r"|\bwhere\s+(do\s+i|can\s+i|to)\b"
    r"|\b(give\s+me|share|provide)\s+(the\s+|a\s+)?(link|url|page|path)"
    r"|\btake\s+me\s+(to|there)\b"
    r"|\bnavigate\s+to\b"
    r"|\b(go|move)\s+to\s+\w"
    r"|\bopen\s+(the\s+)?(page|form|new)\b"
    r"|\b(redirect|guide)\s+me\b"
    r"|\blink\s+(to|for|of)\b"
    # "I want to / I need to / let me + action-verb": action verbs are constrained
    # so this doesn't swallow questions like "I want to know workflow of PO"
    r"|\b(want|need)\s+to\s+(import|export|create|make|raise|add|setup|configure|reconcile|transfer|move|generate|file)\b"
    r"|\bprocess\s+(to|of|for)\s+(import|export|create|make|raise|add|setup|configure|reconcile|transfer|move|generate|file|return|pay)\b"
    r"|\blet\s+me\s+(import|export|create|make|raise|add)\b"
    # Bare references to known how-to subjects — the howto map will resolve them
    r"|\bdata\s+import\b"
    r"|\bstock\s+reconciliation\b"
    r"|\bmaterial\s+(transfer|issue|receipt)\b)",
    re.I,
)

# Write-action requests we should refuse-with-deep-link (read-only bot)
_WRITE_ACTION_RE = re.compile(
    r"\b(create|make|add|new|register|raise|generate|file|submit|update|edit|modify|delete|cancel|amend|approve|reject)\b",
    re.I,
)

# "List all <something>" — small set with hand-written queries (no LLM SQL)
_LIST_USERS_RE     = re.compile(r"\b(list|show|give\s+me)\s+(of\s+|me\s+)?(all\s+)?(active\s+)?users?\b", re.I)
_LIST_WAREHOUSES_RE = re.compile(r"\b(list|show|give\s+me)\s+(of\s+|me\s+)?(all\s+)?warehouses?\b", re.I)
_LIST_ROLES_RE     = re.compile(r"\b(list|show|give\s+me)\s+(of\s+|me\s+)?(all\s+)?roles?\b", re.I)

# Saved-report routes: must beat NL→SQL so "show sales register report" doesn't get translated
_REPORT_LIST_RE = re.compile(
    r"(\b(list|show|give\s+me)\s+(of\s+|me\s+)?(all\s+)?(saved\s+|available\s+)?reports?\b"
    r"|\bwhat\s+reports?\s+(are\s+)?(available|exist)\b"
    r"|\bavailable\s+reports?\b"
    r"|\breports?\s+(for|on)\s+\w)",
    re.I,
)
_REPORT_RUN_RE = re.compile(
    r"(\b(run|execute|generate|fetch|open)\s+(the\s+)?[\w\- ]+?\s+report\b"
    r"|\breport\s*[-–:]\s*\S"
    r"|\b(show|give\s+me)\s+(the\s+)?[\w\- ]+?\s+report\b)",
    re.I,
)

# Schema reflection: "what fields does Delivery Note have", "columns of Sales Order"
_DOCTYPE_INFO_RE = re.compile(
    r"(\b(what|which)\s+fields?\s+(does|are\s+in|of)\b"
    r"|\bfields?\s+(of|in|for)\s+(doctype\s+)?\S"
    r"|\bcolumns?\s+(of|in|for)\s+(doctype\s+)?\S"
    r"|\bschema\s+(of|for)\s+(doctype\s+)?\S"
    r"|\bstructure\s+of\s+(doctype\s+)?\S"
    r"|\bdescribe\s+(doctype\s+)?\S)",
    re.I,
)

# NL-to-SQL: patterns that signal the user wants a live DB query
# Bug fix: added total number, count(*) literal, no of, # of patterns
_NL_QUERY_RE = re.compile(
    r"(how\s+many\b"
    r"|\bcount\s+(of|all|\*)"
    r"|\bcount\s*\(\s*\*\s*\)"
    r"|\btotal\s+(number|count|amount)\b"
    r"|\bno\s+(of|\.)\s+\w"
    r"|\bnumber\s+of\b"
    r"|\bquery\s+(the\s+)?(db|database|mariadb|mysql)\b"
    r"|\brun\s+(a\s+)?query\b"
    r"|\bselect\b.+\bfrom\b"
    r"|\blist\s+all\s+(records|documents|entries)\b"
    r"|\b(created|submitted|cancelled)\s+(till|until|so\s+far|to\s+date)\b)",
    re.I,
)

# Roles that are allowed to run arbitrary SELECT queries
_DB_QUERY_ROLES = {
    "System Manager", "Stock Manager", "Accounts Manager",
    "Purchase Manager", "Sales Manager",
}

# Detect SQL that would dump an entire table without aggregating or filtering.
# Matches `SELECT * FROM tabX [LIMIT N]` (with optional ORDER BY).
_SCHEMA_DUMP_RE = re.compile(
    r"""
    ^\s*SELECT\s+\*\s+FROM\s+`?[\w\s]+`?
    (?:\s+ORDER\s+BY\s+\S+(?:\s+(?:ASC|DESC))?)?
    (?:\s+LIMIT\s+\d+)?\s*;?\s*$
    """,
    re.I | re.X,
)


# SQL syntax breaks that must NOT be swallowed when greedily capturing a
# multi-word table name. Single-word keywords listed here are unambiguous;
# words like ORDER and GROUP are common in Frappe doctype names ("Sales Order",
# "Item Group") so they're matched only in their multi-word clause forms.
_SQL_STOP_RE = (
    r"WHERE|HAVING|LIMIT|UNION|SELECT|FROM|AS|USING|AND|OR|NOT|IN|IS|NULL|BETWEEN|LIKE|ON|JOIN"
    r"|ORDER\s+BY|GROUP\s+BY"
    r"|LEFT\s+JOIN|RIGHT\s+JOIN|INNER\s+JOIN|OUTER\s+JOIN|CROSS\s+JOIN"
)
# Match `FROM/JOIN <Title-Case multi-word ident>` not already wrapped in backticks.
# Uppercase-letter checks use `(?-i:[A-Z])` to stay case-sensitive even though the
# pattern as a whole is re.I (so FROM/JOIN/keywords work both cases). Without
# this, lowercase aliases like `a`, `po` get swallowed into the table-name capture.
_UNQUOTED_TABLE_RE = re.compile(
    r"\b(FROM|JOIN)\s+"
    r"(?!`)"
    r"(?:tab)?"
    r"((?-i:[A-Z])\w*(?:\s+(?!(?:" + _SQL_STOP_RE + r")\b)(?-i:[A-Z])\w*)+)"
    r"(?=\s|$|;|,)",
    re.I,
)


def _backtick_unquoted_tables(sql: str) -> str:
    """
    LLM occasionally emits multi-word table names without backticks
    (e.g. `FROM Asset Revaluation WHERE …`), producing a MariaDB syntax error.
    Wrap any unquoted Title-Case multi-word identifier following FROM/JOIN
    with backticks, prepending `tab` if the LLM also dropped that prefix.
    """
    def _wrap(m: re.Match) -> str:
        # group(2) excludes any leading `tab` (consumed by the prefix in the regex),
        # so always normalise to `tabFoo Bar` form.
        keyword = m.group(1)
        name = re.sub(r"\s+", " ", m.group(2).strip())
        return f"{keyword} `tab{name}`"
    return _UNQUOTED_TABLE_RE.sub(_wrap, sql)


def _is_schema_dump(sql: str) -> bool:
    """True if the SQL is `SELECT * FROM <table>` with no WHERE/aggregate — a column dump."""
    s = sql.strip().rstrip(";")
    if _SCHEMA_DUMP_RE.match(s):
        return True
    # Defense: `SELECT col1, col2, ... FROM tabX` (no WHERE, no aggregate, no GROUP BY)
    # with > 5 columns is also schema-dump-like.
    if re.search(r"\bWHERE\b|\bCOUNT\s*\(|\bSUM\s*\(|\bAVG\s*\(|\bMAX\s*\(|\bMIN\s*\(|\bGROUP\s+BY\b", s, re.I):
        return False
    cols_match = re.match(r"^\s*SELECT\s+(.+?)\s+FROM\b", s, re.I | re.S)
    if cols_match and cols_match.group(1).count(",") >= 5:
        return True
    return False

# SchemaRetriever is initialised in main.py lifespan and injected via route_query().
# This avoids a circular import and keeps schema logic in one place.


_SQL_GEN_PROMPT = """\
You are a MariaDB SQL expert for a Frappe/ERPNext system.
Frappe naming convention: DocType "Delivery Note" → table `tabDelivery Note`.

Common docstatus values: 0 = Draft, 1 = Submitted, 2 = Cancelled.

IMPORTANT: Use ONLY the column names listed below. Do NOT invent columns.
If a concept (e.g. "internal supplier") is not in the column list, use the closest
real column or omit that filter and note it in a comment.

Real table schemas (table: col1, col2, ...):
{schema}

Write a single safe SELECT query for the following question.
- For "how many" / "count" questions → use COUNT(*) or COUNT(`name`).
- For "total amount" / "sum" / "value" questions → use SUM(`grand_total`) or SUM(`rounded_total`), NOT COUNT.
- When the user asks for "amount" or "value", always use SUM on a monetary column (grand_total, rounded_total, base_grand_total, total, net_total), not COUNT.
- Do NOT use INSERT, UPDATE, DELETE, DROP, or any DML/DDL.
- Do NOT include a LIMIT clause (one will be added automatically).
- Return ONLY the raw SQL statement, no explanation, no markdown fences.

Question: {question}
SQL:"""


# ---------------------------------------------------------------------------
# LLM classifier (fallback)
# ---------------------------------------------------------------------------

_CLASSIFIER_PROMPT = """\
You are an intent classifier for an ERP support chatbot. Given a question and optional conversation history, classify the intent and extract parameters.

Intents:
- user_roles: asking about roles assigned to a user. params: username (email or null for self)
- role_perms: asking about what permissions a role has. params: role (role name), doctype (optional filter)
- doctype_roles: asking which roles have access to a doctype. params: doctype (name or abbreviation)
- access_check: asking if a user can perform an action on a doctype. params: username, doctype, permission (read/write/create/delete/submit/cancel)
- tasks: asking about open tasks or todos. params: {{}}
- stock: asking about stock or inventory balance for an item. params: item_code
- users_with_role: asking which users have a specific role. params: {{"role": "role name"}}
- db_query: asking for a live count, total, or data query against the ERP database. params: {{}}
- pending_approvals: asking what documents are pending the user's approval or action. params: {{}}
- doc_lookup: asking to show or fetch details of a specific document by its name (e.g. SO-2024-00123). params: {{"docname": "..."}}
- user_status: asking if a user is enabled/active/disabled. params: {{"username": "partial email or name"}}
- my_username: asking what their own username/email is. params: {{}}
- greeting: saying hi, hello, hey. params: {{}}
- capabilities: asking what the chatbot can do or help with. params: {{}}
- rag: general ERP documentation or process question not covered above. params: {{}}

Recent conversation history (may be empty):
{history}

Question: {question}

Respond with ONLY valid JSON, no explanation:
{{"intent": "<intent>", "params": {{"username": null, "role": null, "doctype": null, "permission": null, "item_code": null}}}}"""


def _llm_classify(question: str, history: str = "") -> dict | None:
    prompt = _CLASSIFIER_PROMPT.format(history=history or "(none)", question=question)
    try:
        resp = httpx.post(
            f"{OLLAMA_BASE_URL}/api/generate",
            json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False, "format": "json"},
            timeout=30.0,
        )
        resp.raise_for_status()
        raw = resp.json().get("response", "")
        data = json.loads(raw)
        if "intent" in data:
            return data
    except Exception as e:
        logger.warning(f"LLM classifier failed: {e}")
    return None


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

def route_query(question: str, username: str, history: str = "", schema_retriever=None,
                user_roles: list[str] | None = None, _skip_compound: bool = False) -> dict | None:
    """
    Returns a result dict if a DB tool handles it, else None (fall through to RAG).
    The result dict includes a `_route` key naming the path taken — main.py stamps
    this into tabAI Chat Log.request_payload for post-hoc analysis.

    `user_roles` is the curated role list from the frontend (preferred over a DB
    fetch, because Administrator legitimately has every role and would otherwise
    drown the workflow handler's "your roles" line).
    `_skip_compound` is set True when the compound splitter recurses into a sub-question,
    so we don't ping-pong forever if a sub-question also looks compound.
    """
    try:
        import db

        # --- Athena Skill pre-step: admin-curated workflow content beats every regex.
        # If admins want to override identity/greeting too, that's a feature — they
        # publish a skill with a matching intent_pattern.
        skill = db.find_athena_skill_match(question)
        if skill:
            return {
                "answer": skill.get("content_md") or "",
                "sources": [],
                "_route": f"skill/{skill.get('skill_id', 'unknown')}",
            }

        # --- Regex pass (fast) ---
        if _IDENTITY_RE.search(question):
            return {
                "answer": (
                    "I am **Athena**, an internal ERP support assistant created by **Krupal Vora**. "
                    "I can help you with Frappe/ERPNext roles, permissions, stock, and process questions."
                ),
                "sources": [],
                "_route": "regex/identity",
            }

        if _GREETING_RE.search(question):
            return {
                "answer": (
                    "Hi! I am **Athena**, your ERP support assistant. I can help you with:\n"
                    "- **Roles & Permissions** — your roles, role permissions, doctype access\n"
                    "- **Stock & Inventory** — item stock balance across warehouses\n"
                    "- **Document Lookup** — fetch details of any SO, PO, DN, GRN, etc.\n"
                    "- **Pending Approvals** — documents waiting for your action\n"
                    "- **Workflows** — states, transitions, who can approve\n"
                    "- **How-to & links** — direct deep-links to ERP pages with steps\n"
                    "- **Database Queries** — count, total, or list records from ERP\n\n"
                    "How can I help you today?"
                ),
                "sources": [],
                "_route": "regex/greeting",
            }

        if _CAPABILITIES_RE.search(question):
            return {
                "answer": (
                    "Here's what I can help you with:\n\n"
                    "1. **Roles & Permissions** — \"What roles do I have?\", \"Who can access Sales Order?\"\n"
                    "2. **Stock Balance** — \"Stock of SLR-100W\", \"CCOP-0001-AADITYA POLYMAKE stock\"\n"
                    "3. **Document Lookup** — \"Show me PO-2024-00123\", \"Details of GRN-2026-03990\"\n"
                    "4. **Pending Approvals** — \"What's pending my approval?\"\n"
                    "5. **Workflows** — \"Workflow of Purchase Order\", \"Who can approve GRN?\"\n"
                    "6. **How-to / Links** — \"How to do data import?\", \"Take me to create a new item\"\n"
                    "7. **Database Queries** — \"How many submitted DNs?\", \"Total amount of Purchase Receipts\"\n"
                    "8. **Open Tasks** — \"My open tasks\"\n\n"
                    "Just ask your question naturally!"
                ),
                "sources": [],
                "_route": "regex/capabilities",
            }

        if _USERNAME_RE.search(question):
            return {
                "answer": f"Your username is **{username}**.",
                "sources": [],
                "_route": "regex/username",
            }

        if _USER_STATUS_RE.search(question):
            r = _handle_user_status(question, db); r["_route"] = "regex/user_status"; return r

        # If question refers back to a role ("that role", "it") and history has one,
        # treat as role_perms rather than doctype_roles even if doctype_roles regex fires.
        _refers_to_role = _REFERS_BACK_RE.search(question) and _extract_role_name(question, history)

        if _USERS_WITH_ROLE_RE.search(question):
            r = _handle_users_with_role(question, db); r["_route"] = "regex/users_with_role"; return r

        # --- Compound / multi-part question splitter ---
        # "what is workflow of GRN, and can I make B2B grn?" → run each clause.
        # Only triggers when the question has a clear conjunction AND each part is non-trivial.
        if not _skip_compound and _looks_compound(question):
            parts = _split_compound(question)
            if len(parts) >= 2:
                return _handle_compound(parts, username, history, db, schema_retriever, user_roles)

        if _WORKFLOW_RE.search(question):
            r = _handle_workflow(question, username, db, history, user_roles=user_roles)
            if r:
                r["_route"] = "regex/workflow"
                return r

        # How-to / link intents must beat NL→SQL — phrases like "how to do data import"
        # otherwise get picked up by _NL_QUERY_RE and the LLM dumps the table schema.
        if _HOWTO_RE.search(question) or (
            _WRITE_ACTION_RE.search(question) and _extract_doctype_anywhere(question)
        ):
            r = _handle_howto(question, username, db)
            if r:
                r["_route"] = "regex/howto"
                return r

        # Saved-report routes (must beat list_users / NL→SQL — "show me reports" else
        # would hit list_users via the loose 'show me' prefix).
        if _REPORT_LIST_RE.search(question):
            r = _handle_report_list(question, db)
            if r:
                r["_route"] = "regex/report_list"
                return r
        if _REPORT_RUN_RE.search(question):
            r = _handle_report_run(question, username, db)
            if r:
                r["_route"] = "regex/report_run"
                return r

        # Hand-written list queries — bypass LLM SQL generation
        if _LIST_USERS_RE.search(question):
            r = _handle_list_users(db); r["_route"] = "regex/list_users"; return r
        if _LIST_WAREHOUSES_RE.search(question):
            r = _handle_list_warehouses(db); r["_route"] = "regex/list_warehouses"; return r
        if _LIST_ROLES_RE.search(question):
            r = _handle_list_roles(db); r["_route"] = "regex/list_roles"; return r

        if _DOCTYPE_ROLES_RE.search(question) and not _refers_to_role:
            result = _handle_doctype_roles(question, db, history)
            if result:
                result["_route"] = "regex/doctype_roles"
                return result

        if _ROLE_PERMS_RE.search(question) or _refers_to_role:
            r = _handle_role_permissions(question, db, history); r["_route"] = "regex/role_perms"; return r

        if _USER_ROLES_RE.search(question):
            r = _handle_user_roles(question, username, db, history); r["_route"] = "regex/user_roles"; return r

        if _ACCESS_CHECK_RE.search(question):
            r = _handle_access_check(question, username, db)
            if r:
                r["_route"] = "regex/access_check"
                return r

        if _TASK_RE.search(question):
            r = _handle_tasks(username, db); r["_route"] = "regex/tasks"; return r

        if _STOCK_RE.search(question) and not _PROCESS_INTENT_RE.search(question):
            r = _handle_stock(question, db, history); r["_route"] = "regex/stock"; return r

        if _PENDING_APPROVALS_RE.search(question):
            r = _handle_pending_approvals(username, db); r["_route"] = "regex/pending_approvals"; return r

        if _DOC_LOOKUP_RE.search(question) or _DOCNAME_RE.search(question):
            result = _handle_document_lookup(question, db)
            if result:
                result["_route"] = "regex/doc_lookup"
                return result

        # Doctype info: "what fields does X have" — direct schema lookup beats NL→SQL.
        if _DOCTYPE_INFO_RE.search(question):
            r = _handle_doctype_info(question, db)
            if r:
                r["_route"] = "regex/doctype_info"
                return r

        if _NL_QUERY_RE.search(question):
            r = _handle_nl_query(question, username, db, schema_retriever)
            r["_route"] = "regex/nl_query"
            return r

    except Exception as e:
        logger.warning(f"DB tool (regex path) failed, trying LLM classifier: {e}")

    # --- LLM classifier fallback ---
    classification = _llm_classify(question, history)
    if not classification or classification.get("intent") == "rag":
        return None

    try:
        import db
        r = _dispatch_classified(classification, question, username, db, schema_retriever)
        if r:
            r["_route"] = f"llm/{classification.get('intent', 'unknown')}"
        return r
    except Exception as e:
        logger.warning(f"DB tool (LLM path) failed, falling back to RAG: {e}")

    return None


def _dispatch_classified(classification: dict, question: str, username: str, db, schema_retriever=None) -> dict | None:
    intent = classification.get("intent", "rag")
    params = classification.get("params", {})

    role     = params.get("role")
    doctype  = params.get("doctype")
    item     = params.get("item_code")
    target   = params.get("username") or username
    perm     = params.get("permission", "read")

    if intent == "user_roles":
        return _format_user_roles(target, db.get_user_roles(target), db.get_user_permissions(target))

    if intent == "role_perms" and role:
        rows = db.get_role_doctype_permissions(role, resolve_doctype(doctype) if doctype else None)
        return _format_role_permissions(role, resolve_doctype(doctype) if doctype else None, rows)

    if intent == "doctype_roles" and doctype:
        resolved = resolve_doctype(doctype)
        return _format_doctype_roles(resolved, db.get_doctype_role_permissions(resolved))

    if intent == "access_check" and doctype:
        resolved = resolve_doctype(doctype)
        allowed = db.can_user_access(target, resolved, perm if perm != "view" else "read")
        verb = "can" if allowed else "cannot"
        return {"answer": f"**{target}** **{verb}** {perm} **{resolved}**.", "sources": []}

    if intent == "tasks":
        return _handle_tasks(username, db)

    if intent == "stock" and item:
        return _format_stock(item, db.get_stock_balance(item))

    if intent == "users_with_role" and role:
        users = db.get_users_with_role(role)
        if not users:
            return {"answer": f"No users found with role **{role}**.", "sources": []}
        lines = "\n".join(f"- {u}" for u in users)
        return {"answer": f"Users with role **{role}** ({len(users)} users):\n{lines}", "sources": []}

    if intent == "db_query":
        return _handle_nl_query(question, username, db, schema_retriever)

    if intent == "pending_approvals":
        return _handle_pending_approvals(username, db)

    if intent == "doc_lookup":
        return _handle_document_lookup(question, db)

    if intent == "user_status":
        return _handle_user_status(question, db)

    if intent == "my_username":
        return {"answer": f"Your username is **{username}**.", "sources": []}

    if intent == "greeting":
        return {
            "answer": (
                "Hi! I am **Athena**, your ERP support assistant. "
                "I can help with roles, permissions, stock, document lookups, and more. "
                "How can I help you today?"
            ),
            "sources": [],
        }

    if intent == "capabilities":
        return {
            "answer": (
                "I can help with: roles & permissions, stock balance, document lookups, "
                "pending approvals, database queries, users with roles, open tasks, and ERP process questions. "
                "Just ask naturally!"
            ),
            "sources": [],
        }

    return None


# ---------------------------------------------------------------------------
# Parameter extractors (with history fallback for context resolution)
# ---------------------------------------------------------------------------

# A Frappe role name is always Title Case words: "Purchase Manager", "WH Bulk Return User"
# This pattern captures one or more consecutive Title Case words and stops at lowercase.
_TITLE_CASE_WORDS = r"[A-Z][A-Za-z0-9]*(?:\s+[A-Z][A-Za-z0-9]*)*"


def _extract_role_name(question: str, history: str = "") -> str | None:
    # "role - Purchase Manager" / "role: WH Bulk Return User"
    m = re.search(rf"\brole\s*[-–:]\s*({_TITLE_CASE_WORDS})", question)
    if m:
        return m.group(1).strip()
    # "for role Purchase Manager" / "does role Stock User"
    m = re.search(rf"\b(?:for|of|does)\s+role\s+({_TITLE_CASE_WORDS})", question)
    if m:
        return m.group(1).strip()
    # History fallback: "that role" → look for a role name in history
    if history and _REFERS_BACK_RE.search(question):
        m = re.search(rf"\brole\s*[-–:]\s*({_TITLE_CASE_WORDS})", history)
        if m:
            return m.group(1).strip()
    return None


def _extract_doctype_filter(question: str) -> str | None:
    m = re.search(r"\b(?:on|for|in|doctype)\s+([A-Za-z][A-Za-z0-9 \-]{1,40})(?:\?|$)", question, re.I)
    if m:
        return resolve_doctype(m.group(1).strip())
    return None


_TRAILING_NOISE_RE = re.compile(r"\s+\b(too|also|as well|additionally|either)\b.*$", re.I)

def _extract_doctype_subject(question: str, history: str = "") -> str | None:
    for abbr in DOCTYPE_ALIASES:
        if re.search(rf"\b{re.escape(abbr)}\b", question, re.I):
            return DOCTYPE_ALIASES[abbr]
    m = re.search(
        r"\b(?:for|access|on|to|about)\s+(?:doctype\s+)?([A-Z][A-Za-z ]{2,40}?)(?:\s+what|\s+who|\s+roles?|\?|$)",
        question, re.I,
    )
    if m:
        # Bug fix: strip trailing noise BEFORE resolving, so "Material Request too" → "Material Request"
        raw = _TRAILING_NOISE_RE.sub("", m.group(1)).strip()
        # Also strip common preposition noise at the start of the match
        raw = re.sub(r"^(to|the|a|an)\s+", "", raw, flags=re.I).strip()
        if raw:
            return resolve_doctype(raw)
    # History fallback
    if history and _REFERS_BACK_RE.search(question):
        for abbr in DOCTYPE_ALIASES:
            if re.search(rf"\b{re.escape(abbr)}\b", history, re.I):
                return DOCTYPE_ALIASES[abbr]
    return None


# ---------------------------------------------------------------------------
# Formatters (shared between regex and LLM dispatch paths)
# ---------------------------------------------------------------------------

def _format_user_roles(target: str, roles: list, perms: list) -> dict:
    if not roles:
        return {"answer": f"No roles found for **{target}**.", "sources": []}
    answer = f"**{target}** has {len(roles)} roles:\n" + "\n".join(f"- {r}" for r in roles)
    if perms:
        perm_lines = [f"- {p['allow']}: {p['for_value']}" for p in perms[:10]]
        answer += "\n\nUser Permissions (first 10):\n" + "\n".join(perm_lines)
    return {"answer": answer, "sources": []}


def _format_role_permissions(role: str, doctype_filter: str | None, rows: list) -> dict:
    if not rows:
        scope = f" on **{doctype_filter}**" if doctype_filter else ""
        return {"answer": f"No permissions configured for role **{role}**{scope}.", "sources": []}
    flags_cols = ("read", "write", "create", "delete", "submit", "cancel", "amend")
    by_doctype: dict[str, set] = {}
    for r in rows:
        dt = r["parent"]
        flags = {f for f in flags_cols if r.get(f)}
        by_doctype.setdefault(dt, set()).update(flags)
    lines = [
        f"- **{dt}**: {', '.join(sorted(flags))}"
        for dt, flags in sorted(by_doctype.items()) if flags
    ]
    scope = f" on **{doctype_filter}**" if doctype_filter else f" ({len(lines)} doctypes)"
    return {"answer": f"Permissions for role **{role}**{scope}:\n" + "\n".join(lines), "sources": []}


def _format_doctype_roles(doctype: str, rows: list) -> dict:
    if not rows:
        return {"answer": f"No permissions configured for doctype **{doctype}**.", "sources": []}
    flags_cols = ("read", "write", "create", "delete", "submit", "cancel", "amend")
    by_role: dict[str, set] = {}
    for r in rows:
        flags = {f for f in flags_cols if r.get(f)}
        by_role.setdefault(r["role"], set()).update(flags)
    lines = [
        f"- **{role}**: {', '.join(sorted(flags))}"
        for role, flags in sorted(by_role.items()) if flags
    ]
    return {"answer": f"Roles with access to **{doctype}** ({len(lines)} roles):\n" + "\n".join(lines), "sources": []}


def _fmt_qty(val) -> str:
    """Format Decimal/float: 0E-9 → 0, 3028.000000000 → 3,028"""
    from decimal import Decimal
    try:
        d = Decimal(str(val))
        if d == 0:
            return "0"
        # Remove trailing zeros and show with commas
        return f"{int(d):,}" if d == int(d) else f"{float(d):,.2f}"
    except Exception:
        return str(val)


def _format_stock(item_code: str, rows: list, limit: int | None = None) -> dict:
    if not rows:
        return {"answer": f"No stock records found for item **{item_code}**.", "sources": []}

    # Filter out zero-qty warehouses by default (unless explicit limit requested)
    from decimal import Decimal
    non_zero = [r for r in rows if Decimal(str(r.get("actual_qty", 0))) != 0]
    display_rows = non_zero if non_zero else rows  # show all if everything is zero

    if limit is not None:
        display_rows = sorted(display_rows, key=lambda r: float(r["actual_qty"]), reverse=True)[:limit]
    else:
        display_rows = sorted(display_rows, key=lambda r: float(r["actual_qty"]), reverse=True)

    lines = [
        f"- {r['warehouse']}: **{_fmt_qty(r['actual_qty'])}** (reserved: {_fmt_qty(r['reserved_qty'])})"
        for r in display_rows
    ]
    total_qty = sum(float(r["actual_qty"]) for r in rows)
    summary = f"\n\n**Total across all warehouses:** {_fmt_qty(total_qty)}"
    if len(non_zero) < len(rows):
        summary += f" ({len(rows) - len(non_zero)} zero-stock warehouses hidden)"
    return {"answer": f"Stock balance for **{item_code}**:\n" + "\n".join(lines) + summary, "sources": []}


# ---------------------------------------------------------------------------
# Handlers (thin wrappers over extractors + formatters)
# ---------------------------------------------------------------------------

def _handle_doctype_roles(question: str, db, history: str = "") -> dict | None:
    doctype = _extract_doctype_subject(question, history)
    if not doctype:
        return None
    return _format_doctype_roles(doctype, db.get_doctype_role_permissions(doctype))


def _handle_role_permissions(question: str, db, history: str = "") -> dict:
    role = _extract_role_name(question, history)
    if not role:
        return {"answer": "Please specify the role name, e.g. 'what permissions does role **Purchase Manager** have?'", "sources": []}
    doctype_filter = _extract_doctype_filter(question)
    rows = db.get_role_doctype_permissions(role, doctype_filter)
    return _format_role_permissions(role, doctype_filter, rows)


def _handle_user_roles(question: str, username: str, db, history: str = "") -> dict:
    email_match = re.search(r"[\w.+\-]+@[\w\-]+(?:\.[a-z]+)?", question, re.I)
    target = email_match.group(0) if email_match else username
    roles = db.get_user_roles(target)
    if not roles and "@" in target:
        resolved = db.find_user_by_partial_email(target)
        if resolved:
            target = resolved
            roles = db.get_user_roles(target)
    return _format_user_roles(target, roles, db.get_user_permissions(target))


def _handle_access_check(question: str, username: str, db) -> dict | None:
    email_match = re.search(r"[\w.+\-]+@[\w\-]+(?:\.[a-z]+)?", question, re.I)
    target = email_match.group(0) if email_match else username
    dt_match = re.search(
        r"(access|view|edit|create|delete|submit)\s+([A-Z][A-Za-z ]{2,30}?)(?:\?|$|\s+in\b)", question,
    )
    if not dt_match:
        return None
    doctype = resolve_doctype(dt_match.group(2).strip())
    perm = dt_match.group(1).lower()
    if perm == "view":
        perm = "read"
    allowed = db.can_user_access(target, doctype, perm)
    verb = "can" if allowed else "cannot"
    return {"answer": f"**{target}** **{verb}** {perm} **{doctype}**.", "sources": []}


def _handle_tasks(username: str, db) -> dict:
    tasks = db.get_open_tasks_for_user(username)
    if not tasks:
        return {"answer": f"No open tasks found for **{username}**.", "sources": []}
    lines = []
    for t in tasks[:10]:
        ref = f" ({t['reference_type']} — {t['reference_name']})" if t.get("reference_type") else ""
        desc = (t.get("description") or "")[:80]
        lines.append(f"- [{t.get('priority', 'Medium')}] {desc}{ref}")
    return {"answer": f"Open tasks for **{username}**:\n" + "\n".join(lines), "sources": []}


def _handle_users_with_role(question: str, db) -> dict:
    role = _extract_role_name(question)
    if not role:
        m = re.search(r"\brole\s+([A-Za-z][A-Za-z0-9 \-]+)", question, re.I)
        if m:
            role = m.group(1).strip().title()
    if not role:
        return {"answer": "Please specify a role name, e.g. 'users with role **System Manager**'.", "sources": []}
    users = db.get_users_with_role(role)
    if not users:
        return {"answer": f"No users found with role **{role}**.", "sources": []}
    lines = "\n".join(f"- {u}" for u in users)
    return {"answer": f"Users with role **{role}** ({len(users)} users):\n{lines}", "sources": []}


def _handle_user_status(question: str, db) -> dict:
    """Check if a user is enabled/active in the system."""
    m = re.search(r"\buser\s+([\w.+\-]+(?:@[\w\-]+(?:\.[a-z]+)?)?)", question, re.I)
    if not m:
        return {"answer": "Please specify a user email, e.g. 'is user john@example.com enabled?'", "sources": []}
    partial = m.group(1)
    try:
        with db.db_cursor() as cur:
            cur.execute(
                "SELECT name, full_name, enabled FROM `tabUser` WHERE name LIKE %s LIMIT 5",
                (f"%{partial}%",),
            )
            rows = cur.fetchall()
    except Exception as e:
        return {"answer": f"Could not check user status: {e}", "sources": []}
    if not rows:
        return {"answer": f"No user found matching **{partial}**.", "sources": []}
    lines = []
    for r in rows:
        status = "Enabled" if r["enabled"] else "Disabled"
        lines.append(f"- **{r['name']}** ({r.get('full_name', '')}) — {status}")
    return {"answer": f"User status:\n" + "\n".join(lines), "sources": []}


def _handle_document_lookup(question: str, db) -> dict | None:
    """
    Fetch and display key fields of a specific Frappe document by name.
    Resolves doctype from the docname prefix (SO → Sales Order, etc.).
    """
    m = _DOCNAME_RE.search(question)
    if not m:
        return None
    docname = m.group(1)
    prefix = docname.split("-")[0].upper()
    doctype = DOCTYPE_ALIASES.get(prefix)
    if not doctype:
        return {"answer": f"I don't recognise the document prefix **{prefix}**. Please specify the full doctype.", "sources": []}

    try:
        row = db.get_document(doctype, docname)
    except Exception as e:
        return {"answer": f"Could not fetch **{docname}**: {e}", "sources": []}

    if not row:
        return {"answer": f"Document **{docname}** not found in **{doctype}**.", "sources": []}

    # Display a curated subset of fields — skip internal/long fields
    _SKIP = {"amended_from", "idx", "doctype", "_user_tags", "_comments", "_assign",
              "_liked_by", "naming_series"}
    _LONG_FIELDS = {"description", "terms", "instructions", "remarks", "note"}

    lines = []
    for k, v in row.items():
        if k.startswith("_") or k in _SKIP or v is None or v == "":
            continue
        if k in _LONG_FIELDS and isinstance(v, str) and len(v) > 120:
            v = v[:120] + "…"
        lines.append(f"- **{k}**: {v}")

    docstatus_map = {0: "Draft", 1: "Submitted", 2: "Cancelled"}
    if "docstatus" in row:
        lines = [f"- **Status**: {docstatus_map.get(row['docstatus'], row['docstatus'])}"] + [
            l for l in lines if "docstatus" not in l
        ]

    return {
        "answer": f"**{doctype}** — {docname}\n\n" + "\n".join(lines),
        "sources": [],
    }


def _handle_pending_approvals(username: str, db) -> dict:
    """Return documents pending the user's approval via Workflow Actions."""
    try:
        rows = db.get_pending_approvals(username)
    except Exception as e:
        return {"answer": f"Could not fetch pending approvals: {e}", "sources": []}

    if not rows:
        return {"answer": f"No documents are pending your approval, **{username}**.", "sources": []}

    lines = []
    for r in rows:
        state = f" [{r['workflow_state']}]" if r.get("workflow_state") else ""
        lines.append(f"- **{r['document_type']}** — {r['document_name']}{state} (action: {r.get('action', '?')})")

    return {
        "answer": f"Documents pending your approval ({len(lines)}):\n" + "\n".join(lines),
        "sources": [],
    }


def _handle_nl_query(question: str, username: str, db, schema_retriever=None) -> dict:
    """
    Natural-language → SQL handler.
    1. Checks the user has a role in _DB_QUERY_ROLES.
    2. Retrieves relevant table schemas via SchemaRetriever (semantic search).
    3. Uses the LLM to generate a SELECT query with real columns injected.
    4. Validates + executes it via db.execute_safe_select().
    """
    # --- Access gate ---
    user_roles = set(db.get_user_roles(username))
    if not user_roles.intersection(_DB_QUERY_ROLES):
        return {
            "answer": (
                "You don't have permission to run live database queries. "
                f"Required roles: {', '.join(sorted(_DB_QUERY_ROLES))}."
            ),
            "sources": [],
        }

    # --- Retrieve relevant schema via semantic search ---
    if schema_retriever and schema_retriever.is_ready():
        schema = schema_retriever.get_relevant_schemas(question)
    else:
        schema = "(schema not available — run index_schema.py)"

    prompt = _SQL_GEN_PROMPT.format(schema=schema, question=question)
    try:
        resp = httpx.post(
            f"{OLLAMA_BASE_URL}/api/generate",
            json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
            timeout=120.0,
        )
        resp.raise_for_status()
        sql = resp.json().get("response", "").strip()
    except Exception as e:
        logger.warning(f"SQL generation failed: {e}")
        return {"answer": "Could not generate a database query. Please try rephrasing.", "sources": []}

    # Strip markdown fences if the LLM wrapped the query
    sql = re.sub(r"^```[a-z]*\n?", "", sql, flags=re.I).rstrip("` \n")

    # Repair unquoted multi-word table names — common LLM slip that produces
    # `FROM Asset Revaluation WHERE …`. MariaDB parses up to the first space and
    # then chokes on the second word.
    fixed_sql = _backtick_unquoted_tables(sql)
    if fixed_sql != sql:
        logger.info(f"NL-query auto-backticked tables: {sql!r} -> {fixed_sql!r}")
        sql = fixed_sql

    logger.info(f"NL-query generated SQL: {sql}")

    # Guardrail: reject schema-dump SQL like `SELECT * FROM tabX [LIMIT N]` with
    # no aggregate / WHERE — this is what produced the column dump for
    # "how to do data import" before _HOWTO_RE intercepted it. Defense in depth.
    if _is_schema_dump(sql):
        logger.info(f"Rejecting schema-dump SQL: {sql}")
        return {
            "answer": (
                "I think you wanted documentation rather than a raw table dump. "
                "Try asking *\"how to ...\"* or *\"give me link to ...\"* and I'll "
                "give you the deep-link plus steps."
            ),
            "sources": [],
        }

    # --- Execute ---
    try:
        rows = db.execute_safe_select(sql, limit=100)
    except ValueError as e:
        return {"answer": f"Query rejected: {e}", "sources": []}
    except Exception as e:
        logger.warning(f"NL-query execution failed: {e}")
        return {"answer": f"Query failed: {e}", "sources": []}

    if not rows:
        return {"answer": f"Query returned no results.\n\n```sql\n{sql}\n```", "sources": []}

    # Format: if single cell (e.g. COUNT), return inline; else table-style
    if len(rows) == 1 and len(rows[0]) == 1:
        val = list(rows[0].values())[0]
        return {"answer": f"**Result:** {val}\n\n```sql\n{sql}\n```", "sources": []}

    lines = []
    headers = list(rows[0].keys())
    lines.append("| " + " | ".join(headers) + " |")
    lines.append("| " + " | ".join("---" for _ in headers) + " |")
    for row in rows[:50]:
        lines.append("| " + " | ".join(str(v) for v in row.values()) + " |")
    suffix = f"\n\n_(showing {min(len(rows), 50)} of {len(rows)} rows)_" if len(rows) > 50 else ""
    return {"answer": "\n".join(lines) + suffix + f"\n\n```sql\n{sql}\n```", "sources": []}


# ---------------------------------------------------------------------------
# M1 — Workflow handler
# ---------------------------------------------------------------------------

_DOC_STATUS_LABEL = {0: "Draft", 1: "Submitted", 2: "Cancelled"}


def _handle_workflow(question: str, username: str, db, history: str = "",
                     user_roles: list[str] | None = None) -> dict | None:
    """
    Answers questions like:
      - "workflow of Purchase Order"
      - "states for GRN"
      - "who can approve PO"
      - "list all workflows"
    Reads from tabWorkflow / tabWorkflow Document State / tabWorkflow Transition.
    Filters/highlights transitions by the user's current roles when known.
    """
    # "list all workflows" → enumerate active workflows
    if re.search(r"\blist\s+(of\s+)?(all\s+)?workflows?\b", question, re.I) \
       or re.search(r"\bwhat\s+workflows?\s+(exist|are\s+there)\b", question, re.I):
        rows = db.list_active_workflows()
        if not rows:
            return {"answer": "No active workflows configured.", "sources": []}
        lines = [
            f"- **{r['document_type']}** — workflow: `{r['name']}` "
            f"([open](/app/workflow/{r['name']}))"
            for r in rows
        ]
        return {
            "answer": f"Active workflows ({len(rows)}):\n" + "\n".join(lines),
            "sources": [],
        }

    doctype = _extract_doctype_anywhere(question) or _extract_doctype_anywhere(history or "")
    if not doctype:
        return {
            "answer": (
                "Which doctype's workflow do you want to see? Try "
                "*\"workflow of Purchase Order\"* or *\"who can approve GRN\"*."
            ),
            "sources": [],
        }

    wf = db.get_active_workflow_for_doctype(doctype)
    if not wf:
        return {
            "answer": (
                f"No active workflow is configured for **{doctype}**. "
                f"Documents follow the standard docstatus flow (Draft → Submitted → Cancelled)."
            ),
            "sources": [],
        }

    states = db.get_workflow_states(wf["name"])
    transitions = db.get_workflow_transitions(wf["name"])

    # User's roles (for "who can approve" + scoping the answer).
    # Prefer the curated list from the request (frontend already filtered out
    # internal/system-level roles); only hit the DB as fallback.
    if user_roles:
        roles_set = set(user_roles)
    else:
        try:
            roles_set = set(db.get_user_roles(username)) if username and username != "anonymous" else set()
        except Exception:
            roles_set = set()

    # "Who can approve" — list distinct roles allowed on transitions whose action is approve-like
    if re.search(r"\bwho\s+can\s+approve\b", question, re.I):
        approve_roles = sorted({
            t["allowed"] for t in transitions
            if t.get("allowed") and re.search(r"\bapprov", (t.get("action") or ""), re.I)
        })
        if not approve_roles:
            approve_roles = sorted({t["allowed"] for t in transitions if t.get("allowed")})
        lines = "\n".join(f"- **{r}**" for r in approve_roles)
        return {
            "answer": (
                f"Roles allowed to approve **{doctype}** "
                f"(workflow `{wf['name']}`):\n{lines}"
            ),
            "sources": [],
        }

    # Full workflow rendering — states + transitions, with user-scoped highlights.
    # doc_status comes back from MariaDB as a string ('0','1','2'); cast before lookup.
    state_lines = []
    for s in states:
        try:
            ds_key = int(s.get("doc_status") or 0)
        except (TypeError, ValueError):
            ds_key = -1
        ds = _DOC_STATUS_LABEL.get(ds_key, "?")
        edit_role = s.get("allow_edit") or "—"
        marker = " ← your role" if edit_role in roles_set else ""
        state_lines.append(f"- **{s['state']}** [{ds}] · editable by: `{edit_role}`{marker}")

    your_transitions = []
    other_transitions = []
    for t in transitions:
        cond = (t.get("condition") or "").strip()
        cond_str = f" · _if_ `{cond}`" if cond else ""
        line = (
            f"- **{t['state']}** → _{t['action']}_ → **{t['next_state']}** "
            f"· role: `{t.get('allowed') or '—'}`{cond_str}"
        )
        if t.get("allowed") in roles_set:
            your_transitions.append(line)
        else:
            other_transitions.append(line)

    sections = [f"**Workflow for {doctype}** (`{wf['name']}`)\n"]
    sections.append("**States:**\n" + "\n".join(state_lines))

    if roles_set:
        # Only the roles that actually appear on this workflow are interesting to mention.
        relevant_roles = sorted({
            r for r in roles_set
            if any(t.get("allowed") == r for t in transitions)
            or any(s.get("allow_edit") == r for s in states)
        })
        roles_label = ", ".join(relevant_roles) if relevant_roles else "(none on this workflow)"
        if your_transitions:
            sections.append(
                f"**Transitions you can perform** (your relevant roles: {roles_label}):\n"
                + "\n".join(your_transitions)
            )
        else:
            sections.append(
                f"**Transitions you can perform:** none — none of your roles "
                f"({roles_label}) are on any transition for this workflow."
            )
        if other_transitions:
            sections.append("**Other transitions:**\n" + "\n".join(other_transitions))
    else:
        sections.append("**Transitions:**\n" + "\n".join(your_transitions + other_transitions))

    sections.append(f"[Open workflow definition](/app/workflow/{wf['name']})")
    return {"answer": "\n\n".join(sections), "sources": []}


# ---------------------------------------------------------------------------
# Compound question splitter — handles "X, and Y?" style multi-part asks
# ---------------------------------------------------------------------------

_COMPOUND_SPLIT_RE = re.compile(r"\s*(?:,\s*(?:and|also|then)\b|\band\s+(?:can|is|do|how|what|who|where)\s+)", re.I)
_TRIVIAL_PART_RE = re.compile(r"^\s*(also|too|please|and|then)?\s*$", re.I)


def _looks_compound(question: str) -> bool:
    # Splits on ", and|also|then" with substantial tail, or "and {can|is|do|how|...}".
    # Mid-question `?` used to be a trigger but caused false splits like
    # "what permissions does X have? on BOM" -> ["...have", "on BOM"] which lost
    # the doctype filter. Punctuation alone is too weak a signal.
    if re.search(r",\s*(and|also|then)\b", question, re.I):
        tail = re.split(r",\s*(?:and|also|then)\b", question, maxsplit=1, flags=re.I)
        if len(tail) > 1 and len(tail[1].split()) >= 3:
            return True
    if re.search(r"\band\s+(?:can|is|do|how|what|who|where)\s+", question, re.I):
        return True
    return False


def _split_compound(question: str) -> list[str]:
    parts = [p.strip(" ?.,") for p in _COMPOUND_SPLIT_RE.split(question) if p and not _TRIVIAL_PART_RE.match(p)]
    # De-dupe while preserving order, cap to 3 sub-questions
    seen, out = set(), []
    for p in parts:
        if p and p.lower() not in seen and len(p.split()) >= 2:
            seen.add(p.lower())
            out.append(p)
        if len(out) == 3:
            break
    return out


def _handle_compound(parts: list[str], username: str, history: str, db, schema_retriever,
                     user_roles: list[str] | None = None) -> dict:
    answers = []
    routes = []
    for i, part in enumerate(parts, 1):
        sub = route_query(part, username, history=history,
                          schema_retriever=schema_retriever,
                          user_roles=user_roles, _skip_compound=True)
        if sub is None:
            answers.append(f"**Q{i}: {part}** — I don't have a structured answer for this; check the docs.")
            routes.append("rag")
        else:
            answers.append(f"**Q{i}: {part}**\n{sub.get('answer', '')}")
            routes.append(sub.get("_route", "?"))
    return {
        "answer": "\n\n---\n\n".join(answers),
        "sources": [],
        "_route": "regex/compound[" + "+".join(routes) + "]",
    }


# ---------------------------------------------------------------------------
# M2 — How-to / deep-link handler
# ---------------------------------------------------------------------------

# Maps a normalised intent phrase → (link template, ordered steps).
# Link templates may contain {doctype_slug} or {item_code} placeholders.
_HOWTO_INTENTS: list[tuple[re.Pattern, str, str, list[str]]] = [
    (
        re.compile(r"\b(data\s+import|import\s+data|import\s+items?|import\s+records?|bulk\s+import|csv\s+import)\b", re.I),
        "Data Import",
        "/app/data-import/new?reference_doctype={target_doctype}",
        [
            "Pick the target DocType (e.g. Item, Customer, Supplier).",
            "Click **Download Template** to get the CSV with required columns.",
            "Fill in the rows, upload via **Import File**, then click **Start Import**.",
        ],
    ),
    (
        re.compile(r"\bstock\s+reconciliation\b|\breconcile\s+stock\b", re.I),
        "Stock Reconciliation",
        "/app/stock-reconciliation/new",
        [
            "Pick the warehouse and the date for the reconciliation.",
            "Add items with their counted quantity and valuation rate.",
            "Save → Submit. The system creates the corrective Stock Ledger entries.",
        ],
    ),
    (
        re.compile(r"\bstock\s+entry\b|\bmaterial\s+(transfer|issue|receipt)\b|\btransfer\s+(stock|material|item)\b", re.I),
        "Stock Entry",
        "/app/stock-entry/new",
        [
            "Choose **Stock Entry Type** (Material Transfer, Material Issue, Material Receipt, etc.).",
            "Set source / target warehouses and add items with quantities.",
            "Save → Submit to post the stock movement.",
        ],
    ),
    (
        re.compile(r"\b(create|new|add|make|raise)\b[^?\n]{0,30}\bitem\b|\bitem\s+master\b", re.I),
        "Item",
        "/app/item/new?item_code={item_code}",
        [
            "Set Item Code and Item Group (mandatory).",
            "Pick the default UOM (Unit of Measure).",
            "Save. To stock-track it, ensure **Maintain Stock** is checked.",
        ],
    ),
    (
        re.compile(r"\b(create|new|add|make|register)\b[^?\n]{0,30}\b(supplier|vendor)\b", re.I),
        "Supplier",
        "/app/supplier/new",
        [
            "Set Supplier Name and Supplier Group.",
            "Pick the default Currency and Country.",
            "Save. Add Tax IDs / Address from the linked tabs.",
        ],
    ),
    (
        re.compile(r"\b(create|new|add|make|register)\b[^?\n]{0,30}\bcustomer\b", re.I),
        "Customer",
        "/app/customer/new",
        [
            "Set Customer Name and Customer Group.",
            "Pick the Territory and default Currency.",
            "Save. Add Address and Contact via the linked tabs.",
        ],
    ),
    (
        re.compile(r"\b(create|new|raise|make)\b[^?\n]{0,30}\b(purchase\s+order|po)\b", re.I),
        "Purchase Order",
        "/app/purchase-order/new",
        [
            "Pick the Supplier and required-by Date.",
            "Add items with qty + rate (or pull from Material Request / Supplier Quotation).",
            "Save → Submit. The PO will then enter the configured workflow.",
        ],
    ),
    (
        re.compile(r"\b(create|new|raise|make)\b[^?\n]{0,30}\b(sales\s+order|so)\b", re.I),
        "Sales Order",
        "/app/sales-order/new",
        [
            "Pick the Customer and Delivery Date.",
            "Add items with qty + rate.",
            "Save → Submit.",
        ],
    ),
    (
        re.compile(r"\b(create|new|make|raise)\b[^?\n]{0,30}\b(grn|purchase\s+receipt)\b", re.I),
        "Purchase Receipt",
        "/app/purchase-receipt/new",
        [
            "Pull from the source Purchase Order via **Get Items From → Purchase Order**.",
            "Set the receiving warehouse and confirm received quantities.",
            "Save → Submit. Stock is posted to the warehouse on submit.",
        ],
    ),
    (
        re.compile(r"\b(create|new|make|raise)\b[^?\n]{0,30}\b(delivery\s+note|dn)\b", re.I),
        "Delivery Note",
        "/app/delivery-note/new",
        [
            "Pull from the source Sales Order via **Get Items From → Sales Order**.",
            "Set the source warehouse and confirm dispatched quantities.",
            "Save → Submit.",
        ],
    ),
    (
        re.compile(r"\b(create|new|add|make)\b[^?\n]{0,30}\bwarehouse\b", re.I),
        "Warehouse",
        "/app/warehouse/new",
        [
            "Set Warehouse Name and Company.",
            "Pick the Parent Warehouse (group warehouse).",
            "Save. Mark **Is Group** if this will hold child warehouses.",
        ],
    ),
    (
        re.compile(r"\b(create|new|add|register)\b[^?\n]{0,30}\buser\b", re.I),
        "User",
        "/app/user/new",
        [
            "Set Email (this becomes the username) and First Name.",
            "Assign Roles via the **Roles** tab (e.g. Stock User, Purchase User).",
            "Save. The user receives a welcome email with a password setup link.",
        ],
    ),
    (
        re.compile(r"\b(create|new|setup|configure|add|make)\b[^?\n]{0,30}\bworkflow\b", re.I),
        "Workflow",
        "/app/workflow/new",
        [
            "Set Document Type and the Workflow State Field (usually `workflow_state`).",
            "Add States with their docstatus and the role that can edit each state.",
            "Add Transitions: from-state → action → to-state, with the role allowed.",
            "Mark **Is Active** and Save.",
        ],
    ),
    (
        re.compile(r"\b(create|new|add|make)\b[^?\n]{0,30}\brole\b", re.I),
        "Role",
        "/app/role/new",
        [
            "Set the Role Name (e.g. *Warehouse Approver*).",
            "Save, then go to **Role Permissions Manager** to grant DocType access.",
        ],
    ),
]


def _handle_howto(question: str, username: str, db) -> dict | None:
    """Match a how-to / link request against _HOWTO_INTENTS and return link + steps."""
    # Extract a possible item code (e.g. "create item ABC-001")
    item_match = re.search(r"\b([A-Z][A-Z0-9]{1,3}-[A-Z0-9\-]{2,})\b", question)
    item_code = item_match.group(1) if item_match else ""

    # The "target_doctype" for "how to do data import for item" is the trailing doctype
    target_doctype = _extract_doctype_anywhere(question) or "Item"
    target_doctype_slug = _slugify_doctype(target_doctype)

    for pattern, label, link_tpl, steps in _HOWTO_INTENTS:
        if pattern.search(question):
            link = link_tpl.format(
                target_doctype=target_doctype,
                doctype_slug=target_doctype_slug,
                item_code=item_code,
            )
            # Strip empty query params (e.g. ?item_code= when we didn't extract one)
            link = re.sub(r"[?&]\w+=(?=&|$)", "", link).rstrip("?&")
            steps_md = "\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1))
            return {
                "answer": (
                    f"**{label}**\n\n"
                    f"Open it directly: [`{link}`]({link})\n\n"
                    f"Steps:\n{steps_md}\n\n"
                    f"_Note: I am read-only — I cannot create the record for you, "
                    f"but the link above takes you straight to the form._"
                ),
                "sources": [],
            }

    # Generic fallback: question mentions a known doctype and a write verb
    # → build a /app/<slug>/new link.
    if _WRITE_ACTION_RE.search(question):
        dt = _extract_doctype_anywhere(question)
        if dt:
            slug = _slugify_doctype(dt)
            link = f"/app/{slug}/new"
            return {
                "answer": (
                    f"To create a new **{dt}**, open: [`{link}`]({link})\n\n"
                    f"I am read-only and can't create it for you — "
                    f"please fill the form and Save/Submit."
                ),
                "sources": [],
            }

    # Last-resort: the user clearly asked a how-to / link question (matched _HOWTO_RE
    # in the router) but we couldn't pin down the intent. Hand back a brief menu
    # of the most-asked links rather than dropping through to the LLM-classifier
    # greeting fallback.
    return {
        "answer": (
            "I'm not sure which page you want — could you be more specific? "
            "Common destinations:\n"
            "- [Data Import](/app/data-import/new) — bulk-load Items, Customers, Suppliers, etc.\n"
            "- [New Item](/app/item/new) · [New Supplier](/app/supplier/new) · [New Customer](/app/customer/new)\n"
            "- [New Purchase Order](/app/purchase-order/new) · [New Sales Order](/app/sales-order/new)\n"
            "- [New Stock Entry](/app/stock-entry/new) · [Stock Reconciliation](/app/stock-reconciliation/new)\n"
            "- [Workflows](/app/workflow) · [Users](/app/user) · [Warehouses](/app/warehouse)"
        ),
        "sources": [],
    }


# ---------------------------------------------------------------------------
# M3 — Hand-written list handlers (bypass LLM SQL)
# ---------------------------------------------------------------------------

def _handle_list_users(db) -> dict:
    rows = db.list_users(enabled_only=True, limit=200)
    if not rows:
        return {"answer": "No active users found.", "sources": []}
    lines = [f"- **{r['name']}** — {r.get('full_name', '')}" for r in rows[:100]]
    suffix = f"\n\n_Showing first 100 of {len(rows)} active users._" if len(rows) > 100 else f"\n\n_{len(rows)} active users._"
    return {"answer": "Active users:\n" + "\n".join(lines) + suffix, "sources": []}


def _handle_list_warehouses(db) -> dict:
    rows = db.list_warehouses(limit=200)
    if not rows:
        return {"answer": "No warehouses found.", "sources": []}
    lines = [
        f"- **{r['name']}** — {r.get('warehouse_type') or '—'} · {r.get('company') or '—'}"
        for r in rows
    ]
    return {"answer": f"Warehouses ({len(rows)}):\n" + "\n".join(lines), "sources": []}


def _handle_list_roles(db) -> dict:
    rows = db.list_roles(limit=200)
    if not rows:
        return {"answer": "No roles found.", "sources": []}
    lines = [f"- {r['name']}" for r in rows]
    return {"answer": f"Roles ({len(rows)}):\n" + "\n".join(lines), "sources": []}


# ---------------------------------------------------------------------------
# Saved-report handlers
# ---------------------------------------------------------------------------

_REPORT_NAME_RE = re.compile(
    r"(?:run|execute|generate|fetch|show|give\s+me|open)\s+(?:the\s+|me\s+)?"
    r"(?:report\s*[-–:]\s*)?"
    r"([A-Za-z][A-Za-z0-9 \-]+?)\s+report\b",
    re.I,
)
_REPORT_NAME_DASH_RE = re.compile(r"\breport\s*[-–:]\s*([A-Za-z][A-Za-z0-9 \-]+?)(?:\?|$)", re.I)
# Frappe Query Reports embed Jinja-style filter placeholders that PyMySQL cannot bind.
_REPORT_PLACEHOLDER_RE = re.compile(r"%\([\w\s]+\)s|\{\{|\{%")


def _handle_report_list(question: str, db) -> dict | None:
    filter_doctype = _extract_doctype_anywhere(question)
    rows = db.list_saved_reports(filter_doctype=filter_doctype, limit=100)
    scope = f" for **{filter_doctype}**" if filter_doctype else ""
    if not rows:
        return {
            "answer": (
                f"No runnable reports found{scope}. Athena can only execute Query Reports — "
                "Script and Report Builder reports must be run from the ERPNext desk."
            ),
            "sources": [],
        }
    lines = [f"- **{r['name']}** — {r.get('ref_doctype') or '—'}" for r in rows]
    return {
        "answer": f"Query Reports{scope} ({len(rows)}):\n" + "\n".join(lines),
        "sources": [],
    }


def _handle_report_run(question: str, username: str, db) -> dict | None:
    m = _REPORT_NAME_RE.search(question) or _REPORT_NAME_DASH_RE.search(question)
    if not m:
        return None
    name = m.group(1).strip().rstrip("?.")
    if not name:
        return None
    report = db.get_query_report(name)
    if not report:
        return {"answer": f"Report **{name}** not found.", "sources": []}
    if report.get("report_type") != "Query Report":
        return {
            "answer": (
                f"Report **{report['name']}** is a {report.get('report_type')} — "
                "Athena can only execute Query Reports. Open it in the ERPNext desk to run."
            ),
            "sources": [],
        }
    if not (username and username != "anonymous"):
        return {"answer": "Sign in to run reports.", "sources": []}
    sql = report.get("query") or ""
    if _REPORT_PLACEHOLDER_RE.search(sql):
        return {
            "answer": (
                f"Report **{report['name']}** requires filter values — please run it from "
                "the ERPNext desk."
            ),
            "sources": [],
        }
    try:
        rows = db.execute_safe_select(sql, limit=100)
    except ValueError as e:
        return {"answer": f"Cannot run report **{report['name']}**: {e}", "sources": []}
    except Exception as e:
        return {"answer": f"Report **{report['name']}** failed: {e}", "sources": []}
    if not rows:
        return {"answer": f"Report **{report['name']}** returned no rows.", "sources": []}
    headers = list(rows[0].keys())
    body_lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    for r in rows[:25]:
        body_lines.append("| " + " | ".join(str(r.get(h, "")) for h in headers) + " |")
    suffix = f"\n\n_Showing first 25 of {len(rows)} rows._" if len(rows) > 25 else ""
    return {"answer": f"**{report['name']}** ({len(rows)} rows):\n" + "\n".join(body_lines) + suffix, "sources": []}


# ---------------------------------------------------------------------------
# Doctype info handler
# ---------------------------------------------------------------------------

def _handle_doctype_info(question: str, db) -> dict | None:
    doctype = _extract_doctype_anywhere(question)
    if not doctype:
        return None
    info = db.get_doctype_info(doctype)
    if not info:
        return {"answer": f"Doctype **{doctype}** not found.", "sources": []}
    cols = info.get("columns") or []
    flags = []
    if info.get("custom"): flags.append("custom")
    if info.get("istable"): flags.append("child table")
    if info.get("issingle"): flags.append("single")
    flag_str = f" _({', '.join(flags)})_" if flags else ""
    naming = info.get("autoname") or info.get("naming_rule") or "—"
    body = (
        f"**{doctype}**{flag_str}\n"
        f"- Module: {info.get('module') or '—'}\n"
        f"- Naming: `{naming}`\n"
        f"- Fields ({len(cols)}): "
        + (", ".join(f"`{c}`" for c in cols) if cols else "_(none)_")
    )
    return {"answer": body, "sources": []}


def _looks_like_item_code(s: str) -> bool:
    """
    True iff the candidate is a plausible Frappe item code: strictly uppercase
    letters + digits + spaces + hyphens + periods, no lowercase. Without this
    guard, patterns 1-3 (which use re.I) will happily capture English filler
    like "what is total" because re.I makes [A-Z] match lowercase too.
    """
    s = s.strip()
    return bool(s) and re.match(r"^[A-Z0-9][A-Z0-9 \-\.]*$", s) is not None


def _handle_stock(question: str, db, history: str = "") -> dict:
    # Parse optional top-N limit
    limit_match = re.search(r"\btop\s+(\d+)\b", question, re.I)
    limit = int(limit_match.group(1)) if limit_match else None

    item_code = None

    # Pattern 1: "<item code> stock/balance/qty" — item code is everything before the keyword
    # Handles "CCOP-0001-AADITYA POLYMAKE stock"
    m0 = re.search(
        r"^([A-Z0-9][A-Z0-9 \-\.]+?)\s+(?:stock|balance|qty|quantity|inventory)\b",
        question.strip(), re.I,
    )
    if m0 and _looks_like_item_code(m0.group(1)):
        item_code = m0.group(1).strip()

    # Pattern 2: "item code XXXX YYYY" — greedy up to delimiter
    if not item_code:
        m = re.search(
            r"\bitem\s+(?:code\s+)?([A-Z0-9][A-Z0-9 \-\.]+?)(?:\s+in\b|\s+at\b|\s+for\b|\s+warehouse|\s+stock|\?|$)",
            question, re.I,
        )
        if m and _looks_like_item_code(m.group(1)):
            item_code = m.group(1).strip()

    # Pattern 3: "stock of <item>" / "stock for <item>"
    if not item_code:
        m2 = re.search(
            r"\b(?:stock|balance|qty|quantity)\s+(?:of|for)\s+([A-Z0-9][A-Z0-9 \-\.]+?)(?:\s+in\b|\s+at\b|\?|$)",
            question, re.I,
        )
        if m2 and _looks_like_item_code(m2.group(1)):
            item_code = m2.group(1).strip()

    # Pattern 4: uppercase-only token (classic Frappe codes like SLR-100W — no spaces).
    # Only fall back to history when the user is referring back ("its qty", "that
    # item"), otherwise we'd hijack vague queries with leftover codes from earlier
    # turns — e.g. "what is total stock count?" picking up "SSE" or "BOM" from a
    # prior answer.
    if not item_code:
        item_match = re.search(r"\b([A-Z][A-Z0-9\-\.]{2,})\b", question)
        if not item_match and history and _REFERS_BACK_RE.search(question):
            item_match = re.search(r"\b([A-Z][A-Z0-9\-\.]{2,})\b", history)
        item_code = item_match.group(1) if item_match else None

    # If we have a code, look it up directly
    if item_code:
        rows = db.get_stock_balance(item_code)
        if rows:
            return _format_stock(item_code, rows, limit)
        # Exact match found no stock — try fuzzy to check if the code is slightly off
        suggestions = db.search_items_by_name(item_code)
        if suggestions:
            lines = "\n".join(f"- **{s['item_code']}** — {s['item_name']}" for s in suggestions)
            return {
                "answer": (
                    f"No stock records found for **{item_code}**. "
                    f"Did you mean one of these?\n{lines}"
                ),
                "sources": [],
            }
        return _format_stock(item_code, [], limit)

    # No code extracted — try fuzzy name search from the question text
    # Strip common stop words and ERP noise to get a meaningful search term
    _NOISE_RE = re.compile(
        r"\b(stock|balance|qty|quantity|bin|warehouse|inventory|check|show|what|is|the|of|for|in|at|how|much|many)\b",
        re.I,
    )
    search_term = _NOISE_RE.sub("", question).strip(" ?")
    search_term = re.sub(r"\s{2,}", " ", search_term).strip()

    if len(search_term) >= 3:
        try:
            suggestions = db.search_items_by_name(search_term)
        except Exception:
            suggestions = []

        if len(suggestions) == 1:
            # Only one match — use it directly
            item_code = suggestions[0]["item_code"]
            return _format_stock(item_code, db.get_stock_balance(item_code), limit)

        if suggestions:
            lines = "\n".join(f"- **{s['item_code']}** — {s['item_name']}" for s in suggestions)
            return {
                "answer": (
                    f"I found multiple items matching **{search_term}**. "
                    f"Which one did you mean?\n{lines}\n\n"
                    "Reply with the exact item code to get the stock balance."
                ),
                "sources": [],
            }

    return {
        "answer": (
            "Please specify an item code or name to check stock balance. "
            "Example: *stock balance for item code SLR-100W* or *stock of Solar Panel 100W*"
        ),
        "sources": [],
    }


# ---------------------------------------------------------------------------
# Source relevance check
# ---------------------------------------------------------------------------

_WANT_SOURCES_RE = re.compile(
    r"\b(source|sources|reference|references|where did you|show.*(doc|link|ref))\b", re.I
)

def wants_sources(question: str) -> bool:
    return bool(_WANT_SOURCES_RE.search(question))
