"""
Direct MariaDB connector for Frappe/ERPNext.
Reads credentials from /db/config.json (mounted via docker-compose).
All queries are raw SQL — no Frappe runtime required.
"""

import json
import os
import re
import logging
from contextlib import contextmanager
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
