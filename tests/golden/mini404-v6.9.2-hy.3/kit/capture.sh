#!/bin/bash
# Captures the responses in this folder from the Mini404 emulator's PrusaLink.
# GET and HEAD requests only. The password is read from a file and reaches curl
# through its config on stdin, never on a command line.
#
# Usage: capture.sh PASSWORD_FILE OUT_DIR [BASE_URL]   (default http://127.0.0.1:8080)
PWFILE="$1"
OUT="$2"
BASE="${3:-http://127.0.0.1:8080}"
if [ ! -r "$PWFILE" ] || [ -z "$OUT" ]; then
    echo "usage: capture.sh PASSWORD_FILE OUT_DIR [BASE_URL]" >&2
    exit 2
fi
mkdir -p "$OUT" || exit 1
PW="$(head -n 1 "$PWFILE" | tr -d '\r\n')"
: > "$OUT/capture.log" || exit 1

# fetch NAME PATH [curl config lines...] -- [curl options...]
fetch() {
    local name="$1" path="$2" config="" out
    shift 2
    while [ $# -gt 0 ] && [ "$1" != "--" ]; do
        config+="$1"$'\n'
        shift
    done
    shift
    out="$(printf '%s' "$config" | curl -sS -m 60 -K - -D "$OUT/$name.headers" -o "$OUT/$name.body" \
        -w '%{http_code} %{time_total}' "$@" "$BASE$path")" || { echo "$name failed" >&2; exit 1; }
    echo "$name $out" | tee -a "$OUT/capture.log"
}

DIGEST="user = \"maker:$PW\""
fetch version          /api/version      "$DIGEST" -- --digest
fetch v1-info          /api/v1/info      "$DIGEST" -- --digest
fetch v1-status        /api/v1/status    "$DIGEST" -- --digest
fetch v1-storage       /api/v1/storage   "$DIGEST" -- --digest
fetch v1-job           /api/v1/job       "$DIGEST" -- --digest
fetch v1-tools         /api/v1/tools     "$DIGEST" -- --digest
fetch v1-tools-http10  /api/v1/tools     "$DIGEST" -- --digest --http1.0
fetch unauth           /api/v1/tools     --
fetch unauth-json      /api/v1/tools     -- -H 'Accept: application/json'
fetch wrong-password   /api/v1/tools     'user = "maker:wrong-password"' -- --digest
fetch wrong-user       /api/v1/tools     "user = \"admin:$PW\"" -- --digest
fetch apikey           /api/v1/tools     "header = \"X-Api-Key: $PW\"" --
fetch apikey-wrong     /api/v1/tools     -- -H 'X-Api-Key: wrong-key'
fetch notfound-v1      /api/v1/nope      "$DIGEST" -- --digest
fetch notfound-v1-json /api/v1/nope      "$DIGEST" -- --digest -H 'Accept: application/json'
fetch notfound-root    /nope             --
fetch files-missing    /api/v1/files/usb/does-not-exist.gcode "$DIGEST" -- --digest
fetch files-no-storage /api/v1/files     "$DIGEST" -- --digest
fetch head-tools       /api/v1/tools     "$DIGEST" -- --digest -I
rm -f "$OUT/head-tools.body"
