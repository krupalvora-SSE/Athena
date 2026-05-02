"""
Direct MariaDB connector for Frappe/ERPNext.
Reads credentials from /db/config.json (mounted via docker-compose).
All queries are raw SQL — no Frappe runtime required.
"""

import json
import os
import re
import uuid
import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pymysql
import pymysql.cursors

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    config_path = Path(os.getenv("DB_CONFIG_PATH", "/db/config.json"))
    if not config_path.exists():
        raise FileNotFoundError(f"DB config not found at {config_path}")
    with open(config_path) as f:
        return json.load(f)


_cfg: dict | None = None

def _cfg_get() -> dict:
    global _cfg
    if _cfg is None:
        _cfg = _load_config()
    return _cfg


def get_connection() -> pymysql.Connection:
    cfg = _cfg_get()
    host = os.getenv("DB_HOST", cfg.get("db_host", "mariadb"))
    port = int(os.getenv("DB_PORT", cfg.get("db_port", 3306)))
    return pymysql.connect(
        host=host,
        port=port,
        user=os.getenv("DB_USER", cfg.get("db_name", "")),   # Frappe uses db_name as the DB user too
        password=os.getenv("DB_PASSWORD", cfg.get("db_password", "")),
        database=os.getenv("DB_NAME", cfg.get("db_name", "")),
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=10,
    )


@contextmanager
def db_cursor():
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# RBAC queries
# ---------------------------------------------------------------------------

def get_user_roles(user: str) -> list[str]:
    """Return all roles assigned to a Frappe user."""
    with db_cursor() as cur:
        cur.execute(
            "SELECT role FROM `tabHas Role` WHERE parent = %s AND parenttype = 'User' AND role != 'All'",
            (user,),
        )
        rows = cur.fetchall()
    return [r["role"] for r in rows]


def find_user_by_partial_email(partial: str) -> str | None:
    """Find an enabled user whose name contains the partial email string."""
    with db_cursor() as cur:
        cur.execute(
            "SELECT name FROM `tabUser` WHERE name LIKE %s AND enabled = 1 LIMIT 1",
            (f"%{partial}%",),
        )
        row = cur.fetchone()
    return row["name"] if row else None


def get_users_with_role(role: str) -> list[str]:
    """Return all users assigned to a given role."""
    with db_cursor() as cur:
        cur.execute(
            "SELECT DISTINCT parent FROM `tabHas Role` "
            "WHERE role = %s AND parenttype = 'User' ORDER BY parent",
            (role,),
        )
        rows = cur.fetchall()
    return [r["parent"] for r in rows]


def get_user_permissions(user: str) -> list[dict]:
    """Return User Permission records — document-level access restrictions."""
    with db_cursor() as cur:
        cur.execute(
            "SELECT allow, `for_value`, applicable_for, is_default "
            "FROM `tabUser Permission` WHERE user = %s",
            (user,),
        )
        return cur.fetchall()


def get_role_doctype_permissions(role: str, doctype: str | None = None) -> list[dict]:
    """
    Return permission rows for a role from both tabDocPerm (app-defined) and
    tabCustom DocPerm (UI-defined via Role Permission Manager), merged.
    Fields: parent (doctype), read, write, create, delete, submit, cancel, amend, permlevel.
    """
    cols = "`parent`, `read`, `write`, `create`, `delete`, `submit`, `cancel`, `amend`, `permlevel`"
    results: list[dict] = []
    with db_cursor() as cur:
        for table in ("`tabDocPerm`", "`tabCustom DocPerm`"):
            if doctype:
                cur.execute(
                    f"SELECT {cols} FROM {table} WHERE role = %s AND parent = %s",
                    (role, doctype),
                )
            else:
                cur.execute(
                    f"SELECT {cols} FROM {table} WHERE role = %s",
                    (role,),
                )
            results.extend(cur.fetchall())
    return results


def get_doctype_role_permissions(doctype: str) -> list[dict]:
    """
    Reverse lookup: for a given doctype, return all roles and their permission flags
    from both tabDocPerm and tabCustom DocPerm.
    """
    cols = "`role`, `read`, `write`, `create`, `delete`, `submit`, `cancel`, `amend`, `permlevel`"
    results: list[dict] = []
    with db_cursor() as cur:
        for table in ("`tabDocPerm`", "`tabCustom DocPerm`"):
            cur.execute(
                f"SELECT {cols} FROM {table} WHERE parent = %s AND role != ''",
                (doctype,),
            )
            results.extend(cur.fetchall())
    return results


def can_user_access(user: str, doctype: str, perm: str = "read") -> bool:
    """
    Quick check: does this user have `perm` on `doctype` via any of their roles?
    perm can be: read, write, create, delete, submit, cancel, amend
    """
    roles = get_user_roles(user)
    if not roles:
        return False
    placeholders = ",".join(["%s"] * len(roles))
    with db_cursor() as cur:
        cur.execute(
            f"SELECT COUNT(*) AS cnt FROM `tabDocPerm` "
            f"WHERE role IN ({placeholders}) AND parent = %s AND `{perm}` = 1",
            (*roles, doctype),
        )
        row = cur.fetchone()
    return (row["cnt"] > 0) if row else False


# ---------------------------------------------------------------------------
# Live data queries
# ---------------------------------------------------------------------------

def get_stock_balance(item_code: str, warehouse: str | None = None) -> list[dict]:
    """Return actual_qty, reserved_qty from tabBin."""
    with db_cursor() as cur:
        if warehouse:
            cur.execute(
                "SELECT item_code, warehouse, actual_qty, reserved_qty, projected_qty "
                "FROM `tabBin` WHERE item_code = %s AND warehouse = %s",
                (item_code, warehouse),
            )
        else:
            cur.execute(
                "SELECT item_code, warehouse, actual_qty, reserved_qty, projected_qty "
                "FROM `tabBin` WHERE item_code = %s",
                (item_code,),
            )
        return cur.fetchall()


def get_document_status(doctype: str, docname: str) -> dict | None:
    """Return name, status, workflow_state, docstatus for any document."""
    table = f"tab{doctype}"
    with db_cursor() as cur:
        cur.execute(
            f"SELECT name, status, workflow_state, docstatus, modified, owner "
            f"FROM `{table}` WHERE name = %s",
            (docname,),
        )
        return cur.fetchone()


def get_open_tasks_for_user(user: str) -> list[dict]:
    """Return open ToDo items assigned to a user."""
    with db_cursor() as cur:
        cur.execute(
            "SELECT name, description, reference_type, reference_name, priority, date "
            "FROM `tabToDo` WHERE allocated_to = %s AND status = 'Open' ORDER BY date ASC LIMIT 50",
            (user,),
        )
        return cur.fetchall()


_DANGEROUS_SQL_RE = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|TRUNCATE|ALTER|CREATE|REPLACE|GRANT|REVOKE|CALL|EXEC)\b",
    re.I,
)


def execute_safe_select(sql: str, limit: int = 100) -> list[dict]:
    """
    Execute a user-supplied SELECT query safely.
    - Rejects anything that is not a plain SELECT.
    - Rejects queries containing dangerous DML/DDL keywords.
    - Appends a LIMIT clause if the query doesn't already have one.
    Raises ValueError for unsafe queries.
    """
    stripped = sql.strip().lstrip(";")
    if not re.match(r"^\s*SELECT\b", stripped, re.I):
        raise ValueError("Only SELECT queries are allowed.")
    if _DANGEROUS_SQL_RE.search(stripped):
        raise ValueError("Query contains disallowed keywords.")
    # Inject LIMIT if absent to prevent runaway scans
    if not re.search(r"\bLIMIT\s+\d+", stripped, re.I):
        stripped = stripped.rstrip(";") + f" LIMIT {int(limit)}"
    with db_cursor() as cur:
        cur.execute(stripped)
        return cur.fetchall()


_VALID_LOG_STATUS = {"Success", "Error", "Timeout", "Permission Denied"}


def log_chat(
    user: str,
    question: str,
    answer: str,
    session_id: str = "",
    sources: list[str] | None = None,
    current_doctype: str = "",
    current_doc: str = "",
    route: str = "",
    status: str = "Success",
    execution_ms: int = 0,
) -> str | None:
    """
    Write a chat interaction directly to `tabAI Chat Log` in Frappe MariaDB.
    Returns the generated document name on success, None on failure.

    Frappe naming series: AICL-YYYY-MM-DD-NNNNN (generated locally — no Frappe runtime needed).

    `_route`, `status`, `execution_ms` are stored as flat columns (added by the
    Frappe-side ai_chat_log.json patch), so logs.py --stats can GROUP BY directly.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")
    date_part = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    suffix = uuid.uuid4().hex[:5].upper()
    name = f"AICL-{date_part}-{suffix}"
    sources_str = "\n".join(sources) if sources else ""

    if status not in _VALID_LOG_STATUS:
        status = "Success"
    execution_ms = max(0, int(execution_ms or 0))

    try:
        with db_cursor() as cur:
            cur.execute(
                """
                INSERT INTO `tabAI Chat Log`
                    (name, creation, modified, modified_by, owner, docstatus,
                     user, question, answer, sources, session_id,
                     current_doctype, current_doc, timestamp,
                     `_route`, status, execution_ms)
                VALUES
                    (%s, %s, %s, %s, %s, 0,
                     %s, %s, %s, %s, %s,
                     %s, %s, %s,
                     %s, %s, %s)
                """,
                (
                    name, now, now, user, user,
                    user, question, answer, sources_str, session_id or "",
                    current_doctype or "", current_doc or "", now,
                    route or "", status, execution_ms,
                ),
            )
        return name
    except Exception as e:
        logger.warning(f"tabAI Chat Log write failed: {e}")
        return None


def get_chat_history(session_id: str, n: int = 15) -> list[dict]:
    """
    Return the last n turns for a session from tabAI Chat Log, oldest first.
    Used by main.py to build conversation history for the LLM prompt.
    """
    with db_cursor() as cur:
        cur.execute(
            "SELECT question, answer FROM `tabAI Chat Log` "
            "WHERE session_id = %s ORDER BY creation DESC LIMIT %s",
            (session_id, n),
        )
        rows = cur.fetchall()
    return list(reversed(rows))


def get_table_columns(table: str) -> list[str]:
    """
    Return column names for a Frappe table via DESCRIBE.
    `table` should be the full table name e.g. 'tabDelivery Note'.
    Returns [] if the table doesn't exist.
    """
    with db_cursor() as cur:
        try:
            cur.execute(f"DESCRIBE `{table}`")
            rows = cur.fetchall()
            return [r["Field"] for r in rows]
        except Exception:
            return []


def get_all_table_schemas() -> dict[str, list[str]]:
    """
    Load real column names for every DocType table that exists in the DB,
    plus all custom fields from tabCustom Field.

    Returns a dict: { "tabDelivery Note": ["name", "supplier", ...], ... }

    Called once at container startup so NL-to-SQL always has fresh, accurate schema.
    """
    _INTERNAL = {"_user_tags", "_comments", "_assign", "_liked_by", "idx"}

    # 1. Discover all tables that follow Frappe's `tab*` naming convention
    with db_cursor() as cur:
        cur.execute("SHOW TABLES")
        all_tables = [list(row.values())[0] for row in cur.fetchall()]

    tab_tables = [t for t in all_tables if t.startswith("tab")]

    # 2. DESCRIBE each table — skip system/log tables to keep the schema compact
    _SKIP_SUFFIXES = (
        " Log", " Version", " Activity", " Feed", " Notification",
        " Hook", " Patch", "DocShare", "DocField", "DocPerm",
    )
    schema: dict[str, list[str]] = {}
    with db_cursor() as cur:
        for table in tab_tables:
            if any(table.endswith(s) for s in _SKIP_SUFFIXES):
                continue
            try:
                cur.execute(f"DESCRIBE `{table}`")
                cols = [
                    r["Field"] for r in cur.fetchall()
                    if r["Field"] not in _INTERNAL
                ]
                if cols:
                    schema[table] = cols
            except Exception:
                continue

    # 3. Overlay custom fields so per-company additions are visible
    try:
        with db_cursor() as cur:
            cur.execute(
                "SELECT dt, fieldname FROM `tabCustom Field` "
                " ORDER BY dt, idx"
            )
            for row in cur.fetchall():
                tbl = f"tab{row['dt']}"
                if tbl in schema and row["fieldname"] not in schema[tbl]:
                    schema[tbl].append(row["fieldname"])
    except Exception as e:
        logger.warning(f"Could not load custom fields: {e}")

    logger.info(f"Schema loaded: {len(schema)} tables, "
                f"{sum(len(v) for v in schema.values())} total columns.")
    return schema


def get_document(doctype: str, docname: str) -> dict | None:
    """
    Fetch all fields of a Frappe document by doctype and name.
    Returns a flat dict, or None if not found.
    """
    table = f"tab{doctype}"
    with db_cursor() as cur:
        cur.execute(f"SELECT * FROM `{table}` WHERE name = %s LIMIT 1", (docname,))
        return cur.fetchone()


def get_pending_approvals(user: str) -> list[dict]:
    """
    Return documents pending this user's action via tabWorkflow Action.
    Returns an empty list if the table doesn't exist (older Frappe versions).
    """
    with db_cursor() as cur:
        try:
            cur.execute(
                "SELECT document_type, document_name, action, workflow_state, creation "
                "FROM `tabWorkflow Action` "
                "WHERE user = %s AND status = 'Open' ORDER BY creation ASC LIMIT 50",
                (user,),
            )
            return cur.fetchall()
        except Exception:
            return []


def search_items_by_name(term: str, limit: int = 5) -> list[dict]:
    """
    Fuzzy-search tabItem by item_code or item_name.
    Returns list of {item_code, item_name} dicts (disabled items excluded).
    """
    like = f"%{term}%"
    with db_cursor() as cur:
        cur.execute(
            "SELECT item_code, item_name FROM `tabItem` "
            "WHERE (item_code LIKE %s OR item_name LIKE %s) AND disabled = 0 "
            "ORDER BY item_name LIMIT %s",
            (like, like, limit),
        )
        return cur.fetchall()


# ---------------------------------------------------------------------------
# Workflow queries
# ---------------------------------------------------------------------------

def get_active_workflow_for_doctype(doctype: str) -> dict | None:
    """
    Return the active workflow definition for a given DocType, or None.
    Picks the first active row — Frappe normally enforces one active workflow per doctype.
    """
    with db_cursor() as cur:
        cur.execute(
            "SELECT name, document_type, workflow_state_field, send_email_alert "
            "FROM `tabWorkflow` WHERE document_type = %s AND is_active = 1 LIMIT 1",
            (doctype,),
        )
        return cur.fetchone()


def get_workflow_states(workflow_name: str) -> list[dict]:
    """States for a workflow: state, doc_status, allow_edit (role), update_field/value."""
    with db_cursor() as cur:
        cur.execute(
            "SELECT state, doc_status, allow_edit, update_field, update_value, idx "
            "FROM `tabWorkflow Document State` WHERE parent = %s ORDER BY idx",
            (workflow_name,),
        )
        return cur.fetchall()


def get_workflow_transitions(workflow_name: str) -> list[dict]:
    """Transitions: state, action, next_state, allowed (role), `condition` (Python expr)."""
    with db_cursor() as cur:
        cur.execute(
            "SELECT state, action, next_state, allowed, `condition`, idx "
            "FROM `tabWorkflow Transition` WHERE parent = %s ORDER BY idx",
            (workflow_name,),
        )
        return cur.fetchall()


def list_active_workflows() -> list[dict]:
    """All active workflows with their target doctype."""
    with db_cursor() as cur:
        cur.execute(
            "SELECT name, document_type FROM `tabWorkflow` "
            "WHERE is_active = 1 ORDER BY document_type"
        )
        return cur.fetchall()


# ---------------------------------------------------------------------------
# Hand-written list queries (used by NL guardrail to avoid LLM-generated SQL)
# ---------------------------------------------------------------------------

def list_users(enabled_only: bool = True, limit: int = 100) -> list[dict]:
    extra = "AND enabled = 1 " if enabled_only else ""
    sql = (
        "SELECT name, full_name, enabled FROM `tabUser` "
        f"WHERE name NOT IN ('Administrator', 'Guest') {extra}"
        "ORDER BY full_name LIMIT %s"
    )
    with db_cursor() as cur:
        cur.execute(sql, (limit,))
        return cur.fetchall()


def list_warehouses(limit: int = 200) -> list[dict]:
    with db_cursor() as cur:
        cur.execute(
            "SELECT name, warehouse_type, company, disabled FROM `tabWarehouse` "
            "WHERE disabled = 0 ORDER BY name LIMIT %s",
            (limit,),
        )
        return cur.fetchall()


def list_roles(limit: int = 200) -> list[dict]:
    with db_cursor() as cur:
        cur.execute(
            "SELECT name, disabled FROM `tabRole` "
            "WHERE disabled = 0 AND name NOT IN ('All', 'Guest', 'Administrator') "
            "ORDER BY name LIMIT %s",
            (limit,),
        )
        return cur.fetchall()


# ---------------------------------------------------------------------------
# Athena Skill — admin-curated workflow content (replaces RAG for known topics)
# ---------------------------------------------------------------------------

def find_athena_skill_match(question: str) -> dict | None:
    """
    Sweep Published Workflow skills, return the first whose intent_pattern matches.
    Side effect: increments use_count + last_used on hit. Best-effort — silently
    returns None if the doctype doesn't exist yet (Frappe-side rollout pending).
    """
    try:
        with db_cursor() as cur:
            cur.execute(
                "SELECT name, skill_id, title, intent_pattern, content_md "
                "FROM `tabAthena Skill` "
                "WHERE status = 'Published' AND skill_type = 'Workflow' "
                "AND intent_pattern IS NOT NULL AND intent_pattern != '' "
                "ORDER BY modified DESC"
            )
            skills = cur.fetchall()
    except Exception:
        return None

    for sk in skills:
        try:
            if re.search(sk["intent_pattern"], question, re.I):
                _increment_skill_usage(sk["name"])
                return sk
        except re.error:
            continue
    return None


def _increment_skill_usage(skill_name: str) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")
    try:
        with db_cursor() as cur:
            cur.execute(
                "UPDATE `tabAthena Skill` "
                "SET use_count = COALESCE(use_count, 0) + 1, last_used = %s, modified = %s "
                "WHERE name = %s",
                (now, now, skill_name),
            )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Saved Reports (read-only) — list + execute Query Reports
# ---------------------------------------------------------------------------

def list_saved_reports(
    filter_doctype: str | None = None,
    report_type: str | None = "Query Report",
    limit: int = 50,
) -> list[dict]:
    """
    List enabled `tabReport` rows. Defaults to Query Reports only — they're the
    only kind Athena can actually execute. Pass `report_type=None` to list all.
    """
    sql = (
        "SELECT name, ref_doctype, report_type FROM `tabReport` "
        "WHERE disabled = 0"
    )
    params: list[Any] = []
    if report_type:
        sql += " AND report_type = %s"
        params.append(report_type)
    if filter_doctype:
        sql += " AND ref_doctype = %s"
        params.append(filter_doctype)
    sql += " ORDER BY name LIMIT %s"
    params.append(limit)
    with db_cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def get_query_report(report_name: str) -> dict | None:
    """
    Fetch a Query Report by name (only Query Reports carry runnable SQL).
    Returns None if not found, disabled, or not a Query Report.
    """
    with db_cursor() as cur:
        cur.execute(
            "SELECT name, ref_doctype, report_type, query "
            "FROM `tabReport` WHERE name = %s AND disabled = 0",
            (report_name,),
        )
        row = cur.fetchone()
    if not row or row.get("report_type") != "Query Report" or not row.get("query"):
        return row
    return row


# ---------------------------------------------------------------------------
# Doctype info (schema reflection for a single doctype)
# ---------------------------------------------------------------------------

def get_doctype_info(doctype: str) -> dict | None:
    """
    Return basic metadata for a doctype: module, autoname, naming_rule, custom?,
    plus column list. Returns None if the doctype isn't registered.
    """
    with db_cursor() as cur:
        cur.execute(
            "SELECT name, module, autoname, naming_rule, custom, istable, issingle "
            "FROM `tabDocType` WHERE name = %s",
            (doctype,),
        )
        meta = cur.fetchone()
    if not meta:
        return None
    meta["columns"] = get_table_columns(f"tab{doctype}")
    return meta


def search_doctype(doctype: str, filters: dict[str, Any], fields: list[str] | None = None, limit: int = 20) -> list[dict]:
    """
    Generic single-table fetch.
    filters: {column: value} — all joined with AND.
    fields: list of column names to select (defaults to *)
    """
    table = f"tab{doctype}"
    select = ", ".join(f"`{f}`" for f in fields) if fields else "*"
    where_clause = " AND ".join(f"`{k}` = %s" for k in filters)
    values = list(filters.values())
    sql = f"SELECT {select} FROM `{table}`"
    if where_clause:
        sql += f" WHERE {where_clause}"
    sql += f" LIMIT {int(limit)}"
    with db_cursor() as cur:
        cur.execute(sql, values)
        return cur.fetchall()
