#!/bin/bash
# F1 Instagram - Safe push script
# Switches to qlemql, pushes, then switches back to work account

set -e

PERSONAL="qlemql"
WORK="torder-frontend-daniel"

# Check current active account
CURRENT=$(gh auth status 2>&1 | grep "Active account: true" -B 2 | grep "account" | awk '{print $NF}' | tr -d '()')

echo "[push] Current gh account: $CURRENT"

# Switch to personal if needed
if [ "$CURRENT" != "$PERSONAL" ]; then
  echo "[push] Switching to $PERSONAL..."
  gh auth switch --user "$PERSONAL"
fi

# Push
echo "[push] Pushing to origin..."
git push "$@"

# Switch back to work account
if [ "$CURRENT" != "$PERSONAL" ]; then
  echo "[push] Switching back to $WORK..."
  gh auth switch --user "$WORK"
  echo "[push] Done. Active account restored to $WORK"
else
  echo "[push] Done. (was already on $PERSONAL, no switch back needed)"
fi
