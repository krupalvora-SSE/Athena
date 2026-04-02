# -----------------------------------------------------------------------
# Add this to your solar_square/hooks.py
# -----------------------------------------------------------------------
#
# This triggers a POST to Athena's /admin/sync-schema after every
# bench migrate so the chatbot schema is always in sync with the DB.
#
# Also add athena_url to your site_config.json:
#   bench set-config athena_url "http://localhost:7001"
# -----------------------------------------------------------------------

after_migrate = ["solar_square.utils.sync_athena_schema.sync"]
