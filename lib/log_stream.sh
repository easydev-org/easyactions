#!/bin/sh
# STREAM A COMMAND'S NEWEST OUTPUT TO A CHECK RUN, WITH NO DEPENDENCY BEYOND curl.
#
# THE PROBLEM IS GITHUB'S, NOT THE CALLER'S. A step's Actions log is unreadable until the step
# finishes, and a CI leg can spend three hours inside one step - so "which step is it on" is answered
# only after the answer has stopped mattering. A CHECK RUN's `output.text` IS readable while it runs.
#
# TWO REQUESTS, AND THAT IS THE WHOLE PROTOCOL:
#
#	POST  /repos/$REPO/check-runs          once, status=in_progress -> capture the id
#	PATCH /repos/$REPO/check-runs/$ID      output.text every interval, conclusion at the end
#
# WHY THERE IS NO PYTHON ANY MORE. This action used to ship log_stream.py, and the image that most
# needs live logs is the one that deliberately carries no interpreter - easyvm's driver is Go and
# speaks QMP directly, so `python3` was removed from it on purpose. An action that refuses to start
# there is an action that cannot do the one job it has.
#
# NO jq EITHER. The id is read out of a response this script generated itself, with `sed`. THE ENTIRE
# DEPENDENCY IS `curl`, which every runner has and every image that talks to a registry already needs.
#
# AND IT IS CANCELLABLE. A cancelled leg must stop its machine, not just stop printing: `kill $pid`
# reaches the shell that owns the daemon, and the daemon owns qemu. `kill_tree` walks down with
# `pgrep -P` so the difference between "the log stops" and "the machine stops" is not a discovery made
# during a cancel.
set -eu

name=${INPUT_NAME:-${GITHUB_JOB:-log-stream}}
interval=${INPUT_INTERVAL:-5}
max_bytes=${INPUT_MAX_BYTES:-60000}
token=${INPUT_TOKEN:-${GITHUB_TOKEN:-}}
continue_on_failure=${LC_CONTINUE_ON_FAILURE:-false}
command=${INPUT_COMMAND:-}
api=${GITHUB_API_URL:-https://api.github.com}
repo=${GITHUB_REPOSITORY:-}
sha=${GITHUB_SHA:-}
run_id=${GITHUB_RUN_ID:-}

[ -n "$command" ] || { echo "log-stream: no command to run" >&2; exit 2; }

# GITHUB REFUSES MORE THAN 65535 CHARACTERS, so the cap is not a preference.
[ "$max_bytes" -gt 65000 ] && max_bytes=65000

work=$(mktemp -d)
log="$work/log"
: > "$log"
trap 'rm -rf "$work"' EXIT INT TERM HUP

# json_escape turns a log into the inside of a JSON string.
#
# THE ORDER IS THE WHOLE THING. BACKSLASH FIRST, THEN THE QUOTE: reversed, the backslash this adds
# before a quote would itself be escaped and `a"b` would arrive as garbage that GitHub accepts.
#
# THE NEWLINE AND THE TAB ARE THE TWO THAT ARE KEPT RATHER THAN DELETED. A JSON string may not contain
# a raw control character, and a log is mostly newlines - MEASURED: the first version passed them
# through and every update was refused, once per interval, for the whole run, with nothing in the log
# to say so. The newline needs the `sed` line-join because `sed` reads one line at a time and cannot
# see one; the tab is escaped rather than dropped because a tab in a log means an aligned column.
json_escape() {
    printf '%s' "$1" \
        | tr -d '\000-\010\013\014\016-\037' \
        | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' \
        | sed -e ':a' -e 'N' -e '$!ba' -e 's/\n/\\n/g' -e 's/\t/\\t/g'
}

# --- the check run ------------------------------------------------------------------------------
#
# THE SPACE AFTER THE COLON IS NOT COSMETIC. GitHub answers `{"id": 4242, ...}`, and the first version
# of this regex required no space: the check run WAS created, the script decided it had failed, and the
# command then ran unpublished - the failure mode that looks like "the feature does nothing at all".
if [ -n "$token" ] && [ -n "$repo" ] && [ -n "$sha" ]; then
    id=$(curl -sS -X POST \
        -H "Authorization: Bearer $token" \
        -H "Accept: application/vnd.github+json" \
        -H "Content-Type: application/json" \
        "$api/repos/$repo/check-runs" \
        -d "{\"name\":\"$(json_escape "$name")\",\"head_sha\":\"$sha\",\"status\":\"in_progress\",\"details_url\":\"${GITHUB_SERVER_URL:-https://github.com}/$repo/actions/runs/$run_id\"}" \
        2>/dev/null | sed -n 's/.*"id"[[:space:]]*:[[:space:]]*\([0-9][0-9]*\).*/\1/p' | head -1) || id=""
else
    id=""
fi

if [ -z "$id" ]; then
    # A STREAM THAT CANNOT PUBLISH STILL RUNS THE COMMAND. The log is a convenience; the work is the
    # point. An API outage must not turn into a failed leg.
    echo "log-stream: no check run; running the command without publishing" >&2
    set +e
    sh -c "$command" > "$log" 2>&1
    code=$?
    set -e
    cat "$log"
    [ "$code" -eq 0 ] || [ "$continue_on_failure" = "true" ]
    exit $(( $? == 0 ? 0 : code ))
fi

echo "log-stream: watching at $api/repos/$repo/check-runs/$id" >&2

publish() {
    body=$(tail -c "$max_bytes" "$log" 2>/dev/null || true)
    [ -n "$body" ] || body="(no output yet)"
    curl -sS -X PATCH \
        -H "Authorization: Bearer $token" \
        -H "Accept: application/vnd.github+json" \
        -H "Content-Type: application/json" \
        "$api/repos/$repo/check-runs/$id" \
        -d "{\"output\":{\"title\":\"$(json_escape "$name")\",\"summary\":\"$(wc -c < "$log" | tr -d ' ') bytes\",\"text\":\"$(json_escape "$body")\"}}" \
        >/dev/null 2>&1 || true
}

conclude() {
    code=$1
    conclusion=success
    [ "$code" -eq 0 ] || conclusion=failure
    [ "$continue_on_failure" = "true" ] && conclusion=success
    body=$(tail -c "$max_bytes" "$log" 2>/dev/null || true)
    [ -n "$body" ] || body="(no output)"
    curl -sS -X PATCH \
        -H "Authorization: Bearer $token" \
        -H "Accept: application/vnd.github+json" \
        -H "Content-Type: application/json" \
        "$api/repos/$repo/check-runs/$id" \
        -d "{\"status\":\"completed\",\"conclusion\":\"$conclusion\",\"output\":{\"title\":\"$(json_escape "$name")\",\"summary\":\"exit $code\",\"text\":\"$(json_escape "$body")\"}}" \
        >/dev/null 2>&1 || true
}

# --- the command ---------------------------------------------------------------------------------
#
# A PLAIN BACKGROUND JOB, AND THE CANCEL WALKS THE TREE.
#
#	`set -m`         prints `can't access tty; job control turned off` in a non-interactive shell, and
#	                 a shell OPTION is not a property of the child - the runner decides
#	`setsid`         forks and exits, so `$!` is the wrapper and `wait` returns at once: the stream
#	                 would report success while the leg was still installing
#
# SO IT IS A NORMAL BACKGROUND JOB and the trap kills its descendants itself. `pgrep -P` is in procps,
# which every runner has.
kill_tree() {
    for child in $(pgrep -P "$1" 2>/dev/null || true); do
        kill_tree "$child"
    done
    kill "$1" 2>/dev/null || true
}

child=0
trap 'child=1; kill_tree "$pid"' TERM INT HUP

sh -c "$command" > "$log" 2>&1 &
pid=$!

while kill -0 "$pid" 2>/dev/null; do
    publish
    # THE SLEEP IS THE INTERVAL, AND IT MUST NOT DELAY THE EXIT. POSIX sh cannot interrupt `wait`, so
    # the loop sleeps a second at a time and notices the child each time.
    slept=0
    while [ "$slept" -lt "$interval" ] && kill -0 "$pid" 2>/dev/null; do
        sleep 1
        slept=$((slept + 1))
    done
done

set +e
wait "$pid"
code=$?
set -e
trap - TERM INT HUP

publish
conclude "$code"

# THE LOG ALSO GOES TO THE STEP'S STDOUT, so the Actions log still has it after the run: the check run
# is for watching, the artifact is for reading afterwards.
cat "$log"

# 128 + N WHEN THE CANCEL ARRIVED, which is how a step reports "I was told to stop" rather than
# "the command failed".
[ "$child" = "1" ] && exit 143
if [ "$code" -eq 0 ] || [ "$continue_on_failure" = "true" ]; then
    exit 0
fi
exit "$code"
