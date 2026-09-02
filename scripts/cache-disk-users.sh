#!/usr/bin/env bash
#
# Cache every user account sitting under ~/.veritaslock/users into the local
# `vl` store, by running `vl usr-acct cache` for each one.
#
# Each user directory is expected to contain:
#   username.txt  - the server-side username
#   password.txt  - that account's password
#
# Already-cached users (and users that fail to authenticate) are skipped; the
# script keeps going and prints a summary at the end.
#
# Usage:
#   scripts/cache-disk-users.sh [USERS_DIR]
#
# Env:
#   VL            path to the vl executable (default: .venv/bin/vl, then `vl`)
#   VL_ENV        environment name to pass as --env (default: unset -> vl default)

set -uo pipefail

USERS_DIR="${1:-$HOME/.veritaslock/users}"

# Resolve the vl executable.
if [[ -n "${VL:-}" ]]; then
    :
elif [[ -x ".venv/bin/vl" ]]; then
    VL=".venv/bin/vl"
elif command -v vl >/dev/null 2>&1; then
    VL="vl"
else
    echo "error: could not find a 'vl' executable (set \$VL or activate the venv)" >&2
    exit 1
fi

env_args=()
[[ -n "${VL_ENV:-}" ]] && env_args=(--env "$VL_ENV")

if [[ ! -d "$USERS_DIR" ]]; then
    echo "error: users dir not found: $USERS_DIR" >&2
    exit 1
fi

cached=0 skipped=0 failed=0
failed_names=()

for dir in "$USERS_DIR"/*/; do
    [[ -d "$dir" ]] || continue
    name="$(basename "$dir")"

    if [[ ! -r "$dir/username.txt" || ! -r "$dir/password.txt" ]]; then
        echo "SKIP  $name — missing username.txt/password.txt"
        ((skipped++))
        continue
    fi

    username="$(<"$dir/username.txt")"
    password="$(<"$dir/password.txt")"
    username="${username//[$'\t\r\n ']/}"
    password="${password//[$'\r\n']/}"

    out="$("$VL" usr-acct cache --username "$username" --password "$password" "${env_args[@]}" 2>&1)"
    rc=$?

    if [[ $rc -eq 0 ]]; then
        echo "OK    $username"
        ((cached++))
    elif grep -qiE "already (cached|have)|duplicate" <<<"$out"; then
        echo "EXIST $username — already cached"
        ((skipped++))
    else
        echo "FAIL  $username — ${out//$'\n'/ }"
        ((failed++))
        failed_names+=("$username")
    fi
done

echo
echo "----------------------------------------"
echo "cached:  $cached"
echo "skipped: $skipped"
echo "failed:  $failed"
if ((failed)); then
    printf '  %s\n' "${failed_names[@]}"
    exit 1
fi
