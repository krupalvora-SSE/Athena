"""
Direct MariaDB connector for Frappe/ERPNext.
Reads credentials from /db/config.json (mounted via docker-compose).
All queries are raw SQL — no Frappe runtime required.
"""

import json
import os
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
        connect_timeout=5,
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
