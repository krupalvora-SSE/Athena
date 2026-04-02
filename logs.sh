#!/bin/bash
# Athena log viewer — runs logs.py inside the chatbot container.
# Usage: ./logs.sh [any logs.py arguments]
# Examples:
#   ./logs.sh                          # last 20 rows
#   ./logs.sh -n 50                    # last 50 rows
#   ./logs.sh --stats                  # summary stats
#   ./logs.sh --failed                 # failures only
#   ./logs.sh --since 2026-04-01       # from a date
#   ./logs.sh --user krupal.v@solarsquare.in
#   ./logs.sh -n 10 --json             # raw JSON

CONTAINER="chatbot-api-1"

# Copy latest version of logs.py into the container before running
docker cp "$(dirname "$0")/logs.py" "$CONTAINER":/app/logs.py 2>/dev/null

docker exec "$CONTAINER" python3 /app/logs.py "$@"
