#!/usr/bin/env bash
#
# Back up the parts of a Script2Video Studio deployment that live outside the
# container image: the database, and the object storage directory.
#
# What it does *not* back up, and why that is not an oversight:
#
#   * **The images.** They are rebuildable from the repository and a tag.
#   * **The `.env`.** It holds the keys that decrypt the encrypted provider
#     secrets (V8.4). It must be backed up, but by the operator, out of band — a
#     secret sitting in the same archive as the data it protects is not a backup
#     strategy, it is a single point of compromise with extra steps. Losing
#     `SECRET_ENCRYPTION_KEY` while keeping `encrypted_secrets` means the provider
#     credentials are unrecoverable; losing both means you re-enter them.
#
# The one thing this script cannot check is whether the dump it produced is
# *usable*. It checks that the dump is non-empty and contains a schema, which
# catches the common failure (a container that answered with an error message),
# but a real restore rehearsal is the only proof. `scripts/restore.sh` exists to
# make that rehearsal cheap enough to actually do.
#
# Usage: scripts/backup.sh [options]   (--help for the list)

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

BACKUP_DIR="${REPO_ROOT}/backups"
KEEP=14
COMPOSE_FILE="${REPO_ROOT}/docker-compose.prod.yml"
DB_SERVICE="mysql"
STORAGE_DIR="${REPO_ROOT}/storage"
DO_DB=1
DO_STORAGE=1
DRY_RUN=0
PRUNE=0
RECORD_URL=""
RECORD_TOKEN=""

usage() {
  cat <<'USAGE'
Back up the database and object storage of a Script2Video Studio deployment.

Options:
  -d, --dir DIR        Where to write the archive (default: ./backups)
  -k, --keep N         How many archives to retain (default: 14; 0 disables)
  -c, --compose FILE   Compose file used to reach the database service
                       (default: docker-compose.prod.yml)
      --service NAME   Database service name in that compose file (default: mysql)
      --storage DIR    Object storage directory to archive (default: ./storage)
      --no-db          Skip the database dump
      --no-storage     Skip the storage archive
      --record URL     Report the finished backup to URL/api/deployment/backups,
                       so its age is visible from GET /api/deployment
      --token TOKEN    Bearer token used for --record (an API key with
                       deployment:manage)
      --prune          Delete archives beyond --keep. Without it they are only
                       listed, because this script does not delete data by
                       default and a retention typo should not be able to.
      --dry-run        Print the commands instead of running them
  -h, --help           This message

Exit codes: 0 success, 1 failure, 2 usage error.
USAGE
}

die() { printf 'backup: %s\n' "$1" >&2; exit "${2:-1}"; }

while [ $# -gt 0 ]; do
  case "$1" in
    -d|--dir) BACKUP_DIR="$2"; shift 2 ;;
    -k|--keep) KEEP="$2"; shift 2 ;;
    -c|--compose) COMPOSE_FILE="$2"; shift 2 ;;
    --service) DB_SERVICE="$2"; shift 2 ;;
    --storage) STORAGE_DIR="$2"; shift 2 ;;
    --no-db) DO_DB=0; shift ;;
    --no-storage) DO_STORAGE=0; shift ;;
    --record) RECORD_URL="$2"; shift 2 ;;
    --token) RECORD_TOKEN="$2"; shift 2 ;;
    --prune) PRUNE=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "unknown option: $1" 2 ;;
  esac
done

case "$KEEP" in
  ''|*[!0-9]*) usage >&2; die "--keep must be a non-negative integer" 2 ;;
esac
[ "$DO_DB" -eq 1 ] || [ "$DO_STORAGE" -eq 1 ] || die "nothing to do: --no-db and --no-storage together"

run() {
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '  [dry-run] %s\n' "$*"
  else
    "$@"
  fi
}

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_ID="s2v-backup-${STAMP}"
ARCHIVE="${BACKUP_DIR}/${BACKUP_ID}.tar.gz"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/s2v-backup.XXXXXX")"

cleanup() { rm -rf -- "$WORK"; }
trap cleanup EXIT

printf 'backup: %s\n' "$BACKUP_ID"
printf '  archive   %s\n' "$ARCHIVE"

if [ "$DRY_RUN" -eq 0 ]; then
  mkdir -p -- "$BACKUP_DIR"
fi

# --- database ----------------------------------------------------------------
if [ "$DO_DB" -eq 1 ]; then
  [ -f "$COMPOSE_FILE" ] || die "compose file not found: $COMPOSE_FILE"
  printf '  database  via %s (service %s)\n' "$(basename -- "$COMPOSE_FILE")" "$DB_SERVICE"

  # The credentials are expanded *inside* the container, where the compose file
  # already put them, so they never appear in this host's process list — which is
  # where a `-p"$PASSWORD"` on the command line would put them.
  #
  # ``--single-transaction`` gives a consistent snapshot of InnoDB tables without
  # locking writers, so a backup can be taken while the deployment is serving
  # reads. It is still taken with maintenance mode on in the documented runbook,
  # because consistency of the *database* does not imply consistency with the
  # assets being written alongside it.
  dump_cmd="MYSQL_PWD=\"\${MYSQL_PASSWORD}\" mysqldump \
--single-transaction --routines --triggers --events \
--default-character-set=utf8mb4 -u\"\${MYSQL_USER}\" \"\${MYSQL_DATABASE}\""

  if [ "$DRY_RUN" -eq 1 ]; then
    printf '  [dry-run] docker compose -f %s exec -T %s sh -c %s > %s/database.sql\n' \
      "$COMPOSE_FILE" "$DB_SERVICE" "'$dump_cmd'" "$WORK"
  else
    docker compose -f "$COMPOSE_FILE" exec -T "$DB_SERVICE" \
      sh -c "$dump_cmd" > "${WORK}/database.sql" \
      || die "mysqldump failed (is the '$DB_SERVICE' service running?)"

    # A dump that is a few hundred bytes is an error message, not a schema. This
    # check is crude on purpose: it catches the failure mode that actually
    # happens, and it cannot give false confidence the way a size check alone can.
    dump_bytes="$(wc -c < "${WORK}/database.sql" | tr -d ' ')"
    [ "$dump_bytes" -gt 1024 ] || die "database dump is only ${dump_bytes} bytes; refusing to call that a backup"
    grep -q "CREATE TABLE" "${WORK}/database.sql" \
      || die "database dump contains no CREATE TABLE; refusing to call that a backup"
    printf '  database  %s bytes\n' "$dump_bytes"
  fi
fi

# --- object storage ----------------------------------------------------------
if [ "$DO_STORAGE" -eq 1 ]; then
  if [ -d "$STORAGE_DIR" ]; then
    printf '  storage   %s\n' "$STORAGE_DIR"
    # Archived relative to its parent so the restore side can extract it back
    # into place without having to remember what the directory was called.
    run tar -czf "${WORK}/storage.tar.gz" -C "$(dirname -- "$STORAGE_DIR")" "$(basename -- "$STORAGE_DIR")"
  else
    printf '  storage   skipped (%s does not exist)\n' "$STORAGE_DIR"
  fi
fi

# --- manifest ----------------------------------------------------------------
if [ "$DRY_RUN" -eq 0 ]; then
  {
    printf 'backup_id=%s\n' "$BACKUP_ID"
    printf 'created_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf 'host=%s\n' "$(hostname 2>/dev/null || echo unknown)"
    printf 'storage_dir=%s\n' "$STORAGE_DIR"
    printf 'database_service=%s\n' "$DB_SERVICE"
    printf 'compose_file=%s\n' "$(basename -- "$COMPOSE_FILE")"
    printf 'restore_hint=%s\n' "scripts/restore.sh --archive $(basename -- "$ARCHIVE") --yes"
  } > "${WORK}/MANIFEST"

  # ``-C "$WORK" .`` rather than listing members: every member is relative to the
  # same root, and a missing optional member (--no-storage) cannot make the tar
  # command fail.
  tar -czf "$ARCHIVE" -C "$WORK" .
  sha256="$(sha256sum "$ARCHIVE" 2>/dev/null || shasum -a 256 "$ARCHIVE")"
  printf '%s\n' "${sha256%% *}" > "${ARCHIVE}.sha256"
  printf '  archive   %s (%s)\n' "$ARCHIVE" "$(du -h "$ARCHIVE" | cut -f1)"
  printf '  checksum  %s\n' "${ARCHIVE}.sha256"
fi

# --- retention ---------------------------------------------------------------
# Opt-in deletion, and narrow even then: only files matching this script's own
# naming pattern, only inside the directory it was told to write to, and never
# the archive that was just produced.
#
# The listing is a ``while read`` over ``find | sort -r`` rather than ``mapfile``
# because the latter is bash 4+ and a script that silently mis-parses on the
# operator's laptop is worse than one that does not support a bash 3.2 shortcut.
if [ "$KEEP" -gt 0 ] && [ "$DRY_RUN" -eq 0 ]; then
  index=0
  while IFS= read -r candidate; do
    index=$(( index + 1 ))
    [ "$index" -gt "$KEEP" ] || continue
    if [ "$index" -eq "$(( KEEP + 1 ))" ]; then
      printf '  retention archives beyond --keep %d:\n' "$KEEP"
    fi
    case "$candidate" in
      "$BACKUP_DIR"/s2v-backup-*.tar.gz) ;;
      *) printf '    skipping unexpected path: %s\n' "$candidate"; continue ;;
    esac
    if [ "$PRUNE" -eq 1 ]; then
      rm -f -- "$candidate" "${candidate}.sha256"
      printf '    pruned %s\n' "$(basename -- "$candidate")"
    else
      printf '    would prune %s (pass --prune)\n' "$(basename -- "$candidate")"
    fi
  done < <(find "$BACKUP_DIR" -maxdepth 1 -type f -name 's2v-backup-*.tar.gz' | sort -r)
fi

# --- record ------------------------------------------------------------------
if [ -n "$RECORD_URL" ]; then
  url="${RECORD_URL%/}/api/deployment/backups"
  printf '  record    POST %s\n' "$url"
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '  [dry-run] curl -fsS -X POST %s\n' "$url"
  else
    host_label="$(hostname 2>/dev/null || echo unknown)"
    # Built as an array that always has elements, because bash 3.2 under
    # ``set -u`` treats an empty array expansion as an unbound variable.
    curl_args=(
      -fsS -X POST "$url"
      -H 'Content-Type: application/json'
      -d "{\"backup_id\":\"${BACKUP_ID}\",\"note\":\"${host_label}\"}"
    )
    if [ -n "$RECORD_TOKEN" ]; then
      curl_args+=(-H "Authorization: Bearer ${RECORD_TOKEN}")
    fi
    # Failing to record is a warning, not a failed backup: the data is on disk
    # either way, and exiting non-zero here would make a cron wrapper report a
    # broken backup when what broke was bookkeeping.
    curl "${curl_args[@]}" > /dev/null \
      || printf '  record    FAILED (the backup itself is intact)\n' >&2
  fi
fi

printf 'backup: done\n'
