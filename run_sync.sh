#!/usr/bin/env bash
# Cloud routine entry point (Linux). Run AFTER raw Fairly JSON has been saved to ./raw.
#
#   1. Check out the `exports` branch into ./exports_wt (create it on the first run)
#   2. python3 sync.py   -> ./output (compares against the previous export)
#   3. Commit + push the new CSVs to the `exports` branch
#   4. python3 notify.py -> email the summary
set -euo pipefail
cd "$(dirname "$0")"

EXPORT_BRANCH="${EXPORT_BRANCH:-exports}"
WT=exports_wt

git worktree remove --force "$WT" 2>/dev/null || rm -rf "$WT"
git worktree prune

if git ls-remote --exit-code --heads origin "$EXPORT_BRANCH" >/dev/null 2>&1; then
  git fetch --quiet origin "$EXPORT_BRANCH"
  git worktree add --quiet -B "$EXPORT_BRANCH" "$WT" "origin/$EXPORT_BRANCH"
else
  echo "Branch $EXPORT_BRANCH not found on origin; creating it."
  git worktree add --quiet --detach "$WT"
  (cd "$WT" && git checkout --quiet --orphan "$EXPORT_BRANCH" && git rm -rfq .)
fi

python3 sync.py --raw raw --out output --previous "$WT"

rm -f "$WT"/*.csv "$WT"/summary.json "$WT"/summary.md
cp output/*.csv output/summary.json output/summary.md "$WT"/

(
  cd "$WT"
  git add -A
  if git diff --cached --quiet; then
    echo "Exports unchanged; nothing to commit."
  else
    git -c user.name="Fairly Sync" -c user.email="fairly-sync@users.noreply.github.com" \
      commit --quiet -m "Reservation export $(date -u +%Y-%m-%d)"
    git push --quiet origin "$EXPORT_BRANCH"
    echo "Pushed exports to $EXPORT_BRANCH."
  fi
)

python3 notify.py --out output
