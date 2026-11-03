#!/usr/bin/env bash
#
# Restore a Script2Video Studio deployment from an archive produced by
# `scripts/backup.sh`.
#
# This is the destructive half of the pair, so it refuses to run unless it has
# been told twice: once with `--yes`, and once by the deployment itself, whose
# maintenance switch must be on. The second check is the interesting one. A
# restore run against a live deployment is a race — the app keeps writing to
# tables that are being rewritten underneath it — and the switch that stops those
# writes is the one V8.5 added precisely so that "I am about to do something
# dangerous to this database" is a thing the deployment can be asked about rather
# than a thing the operator is trusted to remember.
#
# What it does *not* do: delete anything that is not in the archive. The database
# side is inherently destructive (the dump drops and recreates the tables it
# contains), but the storage side is additive — files that arrived after the
# backup stay where they are. That is the safe default and it is worth stating,
# because "restore" is often assumed to mean "make the disk look exactly like the
# archive", and this does not mean that. For a clean-slate restore, restore into a
# deployment whose storage directory was just recreated.
#
# Usage: scripts/restore.sh --archive FILE --yes [options]   (--help for the list)

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

ARCHIVE=""
COMPOSE_FILE="${REPO_ROOT}/docker-compose.prod.yml"
DB_SERVICE="mysql"
STORAGE_DIR="${REPO_ROOT}/storage"
BASE_URL=""
TOKEN=""
DO_DB=1
DO_STORAGE=1
DRY_RUN=0
CONFIRMED=0
SKIP_MAINTENANCE_CHECK=0

usage() {
  cat <<'USAGE'
Restore a Script2Video Studio deployment from a backup archive.

Required:
      --archive FILE   The .tar.gz produced by scripts/backup.sh
      --yes            Acknowledge that this overwrites live data

Options:
  -c, --compose FILE   Compose file used to reach the database service
                       (default: docker-compose.prod.yml)
      --service NAME   Database service name in that compose file (default: mysql)
      --storage DIR    Object storage directory to restore into (default: ./storage)
      --base-url URL   Deployment base URL used to confirm maintenance mode is on
                       (e.g. http://127.0.0.1:18080)
      --token TOKEN    Bearer token for that check (needs deployment:read)
      --skip-maintenance-check
                       Proceed even though the deployment has not been put in
                       maintenance mode. Only for restoring into a stopped or
                       scratch deployment.
      --no-db          Do not restore the database
      --no-storage     Do not restore the storage directory
      --dry-run        Print the commands instead of running them
  -h, --help           This message

Exit codes: 0 success, 1 failure, 2 usage error.
USAGE
}

die() { printf 'restore: %s\n' "$1" >&2; exit "${2:-1}"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --archive) ARCHIVE="$2"; shift 2 ;;
    --yes) CONFIRMED=1; shift ;;
    -c|--compose) COMPOSE_FILE="$2"; shift 2 ;;
    --service) DB_SERVICE="$2"; shift 2 ;;
    --storage) STORAGE_DIR="$2"; shift 2 ;;
    --base-url) BASE_URL="$2"; shift 2 ;;
    --token) TOKEN="$2"; shift 2 ;;
    --skip-maintenance-check) SKIP_MAINTENANCE_CHECK=1; shift ;;
    --no-db) DO_DB=0; shift ;;
    --no-storage) DO_STORAGE=0; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "unknown option: $1" 2 ;;
  esac
done

[ -n "$ARCHIVE" ] || { usage >&2; die "--archive is required" 2; }
[ "$CONFIRMED" -eq 1 ] || { usage >&2; die "--yes is required: this overwrites live data" 2; }
[ "$DO_DB" -eq 1 ] || [ "$DO_STORAGE" -eq 1 ] || die "nothing to do: --no-db and --no-storage together"
[ -f "$ARCHIVE" ] || die "archive not found: $ARCHIVE"

run() {
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '  [dry-run] %s\n' "$*"
  else
    "$@"
  fi
}

WORK="$(mktemp -d "${TMPDIR:-/tmp}/s2v-restore.XXXXXX")"
cleanup() { rm -rf -- "$WORK"; }
trap cleanup EXIT

printf 'restore: %s\n' "$ARCHIVE"

# --- 1. integrity ------------------------------------------------------------
if [ -f "${ARCHIVE}.sha256" ]; then
  expected="$(tr -d '[:space:]' < "${ARCHIVE}.sha256")"
  actual="$(sha256sum "$ARCHIVE" 2>/dev/null || shasum -a 256 "$ARCHIVE")"
  actual="${actual%% *}"
  [ "$expected" = "$actual" ] \
    || die "checksum mismatch: archive is ${actual}, sidecar says ${expected}. Refusing to restore from an archive that changed."
  printf '  integrity checksum matches\n'
else
  printf '  integrity no .sha256 sidecar; skipping (consider re-taking the backup)\n'
fi

# --- 2. the deployment must have been put in maintenance mode ----------------
# Checked before anything is written, and before the archive is even unpacked,
# so a refusal costs nothing and happens while the deployment is still untouched.
if [ "$SKIP_MAINTENANCE_CHECK" -eq 1 ]; then
  printf '  safety    maintenance check skipped (--skip-maintenance-check)\n'
else
  [ -n "$BASE_URL" ] || die "set --base-url so maintenance mode can be verified, or pass --skip-maintenance-check"
  probe_url="${BASE_URL%/}/api/deployment"
  printf '  safety    checking %s\n' "$probe_url"
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '  [dry-run] curl -fsS %s\n' "$probe_url"
  else
    curl_args=(-fsS "$probe_url")
    if [ -n "$TOKEN" ]; then
      curl_args+=(-H "Authorization: Bearer ${TOKEN}")
    fi
    posture="$(curl "${curl_args[@]}")" \
      || die "could not read deployment posture from ${probe_url}"
    # Whitespace is stripped before matching because the JSON this reads comes
    # from FastAPI's default encoder, which writes ``"maintenance_mode": true``
    # with a space — and a safety interlock that silently fails to recognise its
    # own deployment is worse than no interlock at all.
    if printf '%s' "$posture" | tr -d '[:space:]' | grep -q '"maintenance_mode":true'; then
      printf '  safety    maintenance mode is on\n'
    else
      die "deployment is NOT in maintenance mode. Enable it first:
    curl -fsS -X POST ${BASE_URL%/}/api/deployment/maintenance \\
      -H 'Content-Type: application/json' -H \"Authorization: Bearer \$TOKEN\" \\
      -d '{\"enabled\": true}'
  (or pass --skip-maintenance-check for a stopped/scratch deployment)"
    fi
  fi
fi

# --- 3. unpack ---------------------------------------------------------------
run tar -xzf "$ARCHIVE" -C "$WORK"
if [ "$DRY_RUN" -eq 0 ]; then
  [ -f "${WORK}/MANIFEST" ] || die "archive has no MANIFEST; it is not a backup produced by backup.sh"
  printf '  manifest  %s\n' "$(tr '\n' ' ' < "${WORK}/MANIFEST")"
fi

# --- 4. database -------------------------------------------------------------
if [ "$DO_DB" -eq 1 ]; then
  if [ "$DRY_RUN" -eq 0 ]; then
    [ -f "${WORK}/database.sql" ] || die "archive has no database.sql (was it taken with --no-db?)"
  fi
  [ -f "$COMPOSE_FILE" ] || die "compose file not found: $COMPOSE_FILE"
  printf '  database  restoring via %s (service %s)\n' "$(basename -- "$COMPOSE_FILE")" "$DB_SERVICE"
  printf '            note: the dump drops and recreates the tables it contains\n'

  restore_cmd="MYSQL_PWD=\"\${MYSQL_PASSWORD}\" mysql \
--default-character-set=utf8mb4 -u\"\${MYSQL_USER}\" \"\${MYSQL_DATABASE}\""

  if [ "$DRY_RUN" -eq 1 ]; then
    printf '  [dry-run] docker compose -f %s exec -T %s sh -c %s < %s/database.sql\n' \
      "$COMPOSE_FILE" "$DB_SERVICE" "'$restore_cmd'" "$WORK"
  else
    docker compose -f "$COMPOSE_FILE" exec -T "$DB_SERVICE" \
      sh -c "$restore_cmd" < "${WORK}/database.sql" \
      || die "restore failed (is the '$DB_SERVICE' service running, and is the database empty enough?)"
  fi
fi

# --- 5. storage --------------------------------------------------------------
if [ "$DO_STORAGE" -eq 1 ]; then
  if [ "$DRY_RUN" -eq 0 ] && [ ! -f "${WORK}/storage.tar.gz" ]; then
    printf '  storage   skipped (the archive was taken with --no-storage)\n'
  elif [ "$DRY_RUN" -eq 0 ]; then
    parent="$(dirname -- "$STORAGE_DIR")"
    [ -d "$parent" ] || die "parent directory does not exist: $parent"
    printf '  storage   extracting into %s (additive: newer files are kept)\n' "$STORAGE_DIR"
    tar -xzf "${WORK}/storage.tar.gz" -C "$parent"
  else
    printf '  [dry-run] tar -xzf %s/storage.tar.gz -C %s\n' "$WORK" "$(dirname -- "$STORAGE_DIR")"
  fi
fi

cat <<'NEXT'

restore: archive applied. The deployment is still in maintenance mode, and the
         schema is only as new as the archive. Finish the procedure:

  1. apply any migrations the archive predates:
       docker compose -f docker-compose.prod.yml run --rm migrate
  2. verify: GET /api/deployment should show migration.current == migration.head
  3. spot-check: open a run board, confirm its assets render
  4. resume writes:
       curl -fsS -X POST <base-url>/api/deployment/maintenance \
         -H 'Content-Type: application/json' -d '{"enabled": false}'
NEXT
