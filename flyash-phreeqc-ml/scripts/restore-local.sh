#!/usr/bin/env sh
set -eu
umask 077

usage() {
    echo "Usage: scripts/restore-local.sh --yes ARCHIVE.tar.gz"
    echo "Restore into durable volumes; existing paths can be overwritten."
    echo "Set WPI_COMPOSE_FILE=docker-compose.server.yml for the hosted stack."
}

if [ "${1:-}" = -h ] || [ "${1:-}" = --help ]; then usage; exit 0; fi
[ "${1:-}" = --yes ] && [ "$#" -eq 2 ] || { usage >&2; exit 64; }
archive=$2
[ -f "$archive" ] || { echo "Archive not found: $archive" >&2; exit 66; }
[ -f "$archive.sha256" ] || {
    echo "Backup checksum sidecar is required: $archive.sha256" >&2
    exit 65
}
expected=$(awk 'NR == 1 { print $1 }' "$archive.sha256" | tr 'A-F' 'a-f')
case "$expected" in
    *[!0-9a-f]*|'') echo "Backup checksum sidecar is malformed." >&2; exit 65 ;;
esac
[ "${#expected}" -eq 64 ] || {
    echo "Backup checksum sidecar is malformed." >&2
    exit 65
}
actual=$(sha256sum "$archive" | awk '{ print $1 }')
[ "$expected" = "$actual" ] || { echo "Backup checksum mismatch." >&2; exit 65; }

members_file=$(mktemp "${TMPDIR:-/tmp}/wpi-restore-members.XXXXXX")
listing_file=$(mktemp "${TMPDIR:-/tmp}/wpi-restore-listing.XXXXXX")
trap 'rm -f "$members_file" "$listing_file"' 0 1 2 15
if ! tar -tzf "$archive" >"$members_file" \
   || ! tar -tvzf "$archive" >"$listing_file"; then
    echo "Backup archive cannot be read safely." >&2
    exit 65
fi
if awk 'substr($1,1,1) != "-" && substr($1,1,1) != "d" { bad=1 } END { exit bad }' \
        "$listing_file"; then :; else
    echo "Archive contains a link or special file; refusing restore." >&2
    exit 65
fi

allowed=true
while IFS= read -r member; do
    case "$member" in
        /*|../*|*/../*|*/..|*\\*) allowed=false; echo "Unsafe archive member." >&2 ;;
        app/outputs/*|app/experiments/*|app/data/processed/*|app/imports/*|var/lib/wpi/resources/*) ;;
        app/outputs|app/experiments|app/data/processed|app/imports|var/lib/wpi/resources) ;;
        *) allowed=false; echo "Unexpected archive member: $member" >&2 ;;
    esac
done <"$members_file"
[ "$allowed" = true ] || exit 65

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
compose_file=${WPI_COMPOSE_FILE:-docker-compose.local.yml}
archive=$(CDPATH= cd -- "$(dirname -- "$archive")" && pwd)/$(basename -- "$archive")
cd "$project_dir"
[ -f "$compose_file" ] || {
    echo "Compose file not found: $compose_file" >&2
    exit 66
}
docker compose -f "$compose_file" stop app
docker compose -f "$compose_file" run --rm --no-deps -T \
    --user 0:0 --cap-add CHOWN --cap-add DAC_OVERRIDE --cap-add FOWNER \
    --entrypoint sh app -c \
    'find /app/outputs /app/experiments /app/data/processed /app/imports /var/lib/wpi/resources -xdev -mindepth 1 -depth -delete'
docker compose -f "$compose_file" run --rm --no-deps -T \
    --user 0:0 --cap-add CHOWN --cap-add DAC_OVERRIDE --cap-add FOWNER \
    -v "$archive:/restore/backup.tar.gz:ro" --entrypoint sh app -c \
    'set -eu; tar --no-same-owner --no-same-permissions -xzf /restore/backup.tar.gz -C /; chown -R 10001:10001 /app/outputs /app/experiments /app/data/processed /app/imports /var/lib/wpi/resources; chmod 0750 /app/outputs /app/experiments /app/data/processed /app/imports /var/lib/wpi/resources'
docker compose -f "$compose_file" up --detach app
echo "Restore completed from $archive. Review the restored workspace before research use."
