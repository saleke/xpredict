#!/usr/bin/env bash
# Deploy-time updater: pick up team pushes and rebuild the service.
#
# Design constraints, all of which come from the operator's requirements:
#   * polls every 5 minutes (driven by the systemd timer in deploy/);
#   * NEVER commits, pushes, resets, stashes or discards anything;
#   * refuses to act on a dirty tree, so local work is never overwritten;
#   * only ever fast-forwards, so history is never rewritten;
#   * a diverged branch is reported, not "fixed" automatically.
#
# Safe to run by hand: deploy/update.sh            (one pass)
#                    deploy/update.sh --status   (report only, change nothing)

set -uo pipefail

REPO="${LISA_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
BRANCH="${LISA_BRANCH:-main}"
LOG="${LISA_UPDATE_LOG:-$REPO/deploy/update.log}"
COMPOSE="${LISA_COMPOSE:-docker-compose}"
# Never let a fetch hang the timer; bound it and treat a timeout as "skip".
FETCH_TIMEOUT="${LISA_FETCH_TIMEOUT:-90}"

STATUS_ONLY=0
[[ "${1:-}" == "--status" ]] && STATUS_ONLY=1

log() { printf '%s [update] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" | tee -a "$LOG"; }

cd "$REPO" || { log "FATAL: cannot enter $REPO"; exit 1; }

current_commit="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
current_subject="$(git log -1 --pretty=%s 2>/dev/null || echo unknown)"
log "--- pass start: repo=$REPO branch=$BRANCH head=$current_commit ($current_subject)"

# 1. Refuse to touch a tree with local modifications.
if [[ -n "$(git status --porcelain 2>/dev/null)" ]]; then
    log "SKIP: working tree has uncommitted changes; not touching it"
    git status --short | head -20 | tee -a "$LOG"
    exit 0
fi

# 2. Fetch the team's work. Bound it so a hung remote cannot stall the timer.
if ! timeout "$FETCH_TIMEOUT" git fetch --quiet origin "$BRANCH" 2>>"$LOG"; then
    log "SKIP: fetch from origin/$BRANCH failed or timed out (${FETCH_TIMEOUT}s)"
    exit 0
fi

remote_commit="$(git rev-parse --short "origin/$BRANCH" 2>/dev/null || echo unknown)"
log "origin/$BRANCH is at $remote_commit"

ahead="$(git rev-list --count "origin/$BRANCH..HEAD" 2>/dev/null || echo 0)"
behind="$(git rev-list --count "HEAD..origin/$BRANCH" 2>/dev/null || echo 0)"

# 3. Nothing to do.
if [[ "$behind" == "0" ]]; then
    if [[ "$ahead" != "0" ]]; then
        log "UP TO DATE: local is $ahead commit(s) ahead of origin/$BRANCH"
        [[ "$STATUS_ONLY" == "1" ]] && log "ahead commits are local-only; they will need pushing by hand"
    else
        log "UP TO DATE: tree matches origin/$BRANCH"
    fi
    exit 0
fi

# 4. Diverged: local commits and new remote commits. Report loudly. The only
#    correct resolutions are a human push or a human merge, and neither is ours
#    to perform.
if [[ "$ahead" != "0" ]]; then
    log "DIVERGED: local is $ahead ahead and $behind behind origin/$BRANCH."
    log "        Refusing to auto-merge. Resolve by hand, then re-run:"
    log "          git -C $REPO log --oneline --left-right HEAD...origin/$BRANCH"
    log "        then either push the local commits or merge origin/$BRANCH into this branch."
    exit 0
fi

# 5. Fast-forward only.
if ! git merge --ff-only "origin/$BRANCH" >>"$LOG" 2>&1; then
    log "ERROR: fast-forward to origin/$BRANCH failed; tree left untouched"
    exit 1
fi
new_commit="$(git rev-parse --short HEAD)"
log "FAST-FORWARDED: $current_commit -> $new_commit"

if [[ "$STATUS_ONLY" == "1" ]]; then
    log "--status given: not rebuilding"
    exit 0
fi

# 6. Rebuild and roll the service. The new code only takes effect after this.
log "rebuilding image and restarting service"
if ! $COMPOSE -f "$REPO/docker-compose.yml" up -d --build --remove-orphans lisa >>"$LOG" 2>&1; then
    log "ERROR: docker-compose up failed; see $LOG"
    exit 1
fi

# 7. Confirm the new container actually serves, otherwise say so loudly.
log "waiting for health check"
healthy=0
for _ in $(seq 1 30); do
    state="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \
        lisa_sports_engine 2>/dev/null || echo unknown)"
    if [[ "$state" == "healthy" ]]; then healthy=1; break; fi
    if [[ "$state" == "unhealthy" ]]; then break; fi
    sleep 2
done

if [[ "$healthy" == "1" ]]; then
    log "DEPLOYED $new_commit: container is healthy"
else
    log "WARNING: deployed $new_commit but health check did not report healthy"
    log "         inspect with: docker logs --tail 100 lisa_sports_engine"
fi
exit 0
