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


def resolve_doctype(token: str) -> str:
    upper = token.strip().upper()
    return DOCTYPE_ALIASES.get(upper, token.strip())


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
    r"|list\s+(all\s+)?roles?"
    r"|roles?\s+(i|for\s+user|assigned\s+to)"
    r"|what\s+all\s+roles?"
    r"|\bgive\s+(me\s+)?list\s+of\s+roles?"
    r"|\broles?\s+(for|of)\s+user\b"
    r"|\buser\b.{1,40}\broles?\b)",
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
- Use COUNT(*) or COUNT(`name`) for totals.
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

def route_query(question: str, username: str, history: str = "", schema_retriever=None) -> dict | None:
    """
    Returns a result dict if a DB tool handles it, else None (fall through to RAG).
    schema_retriever: optional SchemaRetriever instance for NL-to-SQL schema injection.
    """
    try:
        import db

        # --- Regex pass (fast) ---
        if _IDENTITY_RE.search(question):
            return {
                "answer": (
                    "I am **Athena**, an internal ERP support assistant created by **Krupal Vora**. "
                    "I can help you with Frappe/ERPNext roles, permissions, stock, and process questions."
                ),
                "sources": [],
            }

        # If question refers back to a role ("that role", "it") and history has one,
        # treat as role_perms rather than doctype_roles even if doctype_roles regex fires.
        _refers_to_role = _REFERS_BACK_RE.search(question) and _extract_role_name(question, history)

        if _USERS_WITH_ROLE_RE.search(question):
            return _handle_users_with_role(question, db)

        if _DOCTYPE_ROLES_RE.search(question) and not _refers_to_role:
            result = _handle_doctype_roles(question, db, history)
            if result:
                return result

        if _ROLE_PERMS_RE.search(question) or _refers_to_role:
            return _handle_role_permissions(question, db, history)

        if _USER_ROLES_RE.search(question):
            return _handle_user_roles(question, username, db, history)

        if _ACCESS_CHECK_RE.search(question):
            return _handle_access_check(question, username, db)

        if _TASK_RE.search(question):
            return _handle_tasks(username, db)

        if _STOCK_RE.search(question) and not _PROCESS_INTENT_RE.search(question):
            return _handle_stock(question, db, history)

        if _PENDING_APPROVALS_RE.search(question):
            return _handle_pending_approvals(username, db)

        if _DOC_LOOKUP_RE.search(question) or _DOCNAME_RE.search(question):
            result = _handle_document_lookup(question, db)
            if result:
                return result

        if _NL_QUERY_RE.search(question):
            return _handle_nl_query(question, username, db, schema_retriever)

    except Exception as e:
        logger.warning(f"DB tool (regex path) failed, trying LLM classifier: {e}")

    # --- LLM classifier fallback ---
    classification = _llm_classify(question, history)
    if not classification or classification.get("intent") == "rag":
        return None

    try:
        import db
        return _dispatch_classified(classification, question, username, db, schema_retriever)
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


def _format_stock(item_code: str, rows: list, limit: int | None = None) -> dict:
    if not rows:
        return {"answer": f"No stock records found for item **{item_code}**.", "sources": []}
    if limit is not None:
        rows = sorted(rows, key=lambda r: r["actual_qty"], reverse=True)[:limit]
    lines = [f"- {r['warehouse']}: {r['actual_qty']} (reserved: {r['reserved_qty']})" for r in rows]
    return {"answer": f"Stock balance for **{item_code}**:\n" + "\n".join(lines), "sources": []}


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

    logger.info(f"NL-query generated SQL: {sql}")

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


def _handle_stock(question: str, db, history: str = "") -> dict:
    # Parse optional top-N limit
    limit_match = re.search(r"\btop\s+(\d+)\b", question, re.I)
    limit = int(limit_match.group(1)) if limit_match else None

    # Try "item code XXXX YYYY ZZZZ" — greedy up to a warehouse/location delimiter or end
    # Bug fix: previous regex stopped at first space; now captures full multi-word item codes
    m = re.search(
        r"\bitem\s+(?:code\s+)?([A-Z0-9][A-Z0-9 \-\.]+?)(?:\s+in\b|\s+at\b|\s+for\b|\s+warehouse|\?|$)",
        question, re.I,
    )
    if m:
        item_code = m.group(1).strip()
    else:
        # Try "stock of <item>" / "stock for <item>" patterns
        m2 = re.search(
            r"\b(?:stock|balance|qty|quantity)\s+(?:of|for)\s+([A-Z0-9][A-Z0-9 \-\.]+?)(?:\s+in\b|\s+at\b|\?|$)",
            question, re.I,
        )
        if m2:
            item_code = m2.group(1).strip()
        else:
            # Try uppercase-only token (classic Frappe codes like SLR-100W — no spaces)
            item_match = re.search(r"\b([A-Z][A-Z0-9\-\.]{2,})\b", question)
            if not item_match and history:
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
