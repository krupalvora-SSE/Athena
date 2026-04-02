"""
Frappe post-migrate hook — triggers Athena schema re-sync.

After `bench migrate` completes, this function POSTs to Athena's
/admin/sync-schema endpoint so the bot immediately knows about any
new fields, custom fields, or doctypes added by the migration.

Installation (add to your solar_square app):
  1. Copy this file to:
       frappe-bench/apps/solar_square/solar_square/utils/sync_athena_schema.py

  2. Add to solar_square/hooks.py:
       after_migrate = ["solar_square.utils.sync_athena_schema.sync"]

  3. Optionally set athena_url in site_config.json:
       bench set-config athena_url "http://localhost:7001"
     (defaults to http://localhost:7001 if not set)
"""

import frappe
import requests


def sync():
    """Called automatically by Frappe after every bench migrate."""
    athena_url = frappe.conf.get("athena_url", "http://localhost:7001")
    endpoint = f"{athena_url}/admin/sync-schema"

    frappe.logger("athena").info(f"Triggering Athena schema sync at {endpoint}...")
    try:
        resp = requests.post(endpoint, timeout=300)
        resp.raise_for_status()
        data = resp.json()
        frappe.logger("athena").info(
            f"Athena schema sync complete: {data.get('tables_indexed', '?')} tables indexed."
        )
    except requests.exceptions.ConnectionError:
        frappe.logger("athena").warning(
            "Athena is not running — schema sync skipped. "
            "Restart the Athena container to sync the latest schema."
        )
    except Exception as e:
        frappe.logger("athena").warning(f"Athena schema sync failed: {e}")
