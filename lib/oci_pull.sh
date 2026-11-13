#!/bin/sh
# PULL AN OCI ARTIFACT, RESUMING THE BLOB ACROSS RESETS.
#
# `oras pull` HAS NO RESUME. A transfer reset at the other end starts again from byte zero, and a
# 28 GiB blob over a long-lived HTTP/2 stream is reset often enough that several attempts can all fail
# without making net progress:
#
#	attempt 1  03:39:28 -> 03:45:00   stream error: stream ID 1; PROTOCOL_ERROR
#	attempt 2  03:45:05 -> 03:48:58   the same
#	attempt 3  03:49:08 -> 03:55:03   the same
#
# Three attempts of five to six minutes each, and the file never got past the first.
#
# SO THE BLOB IS FETCHED DIRECTLY WITH `Range:`. The registry is a normal OCI distribution endpoint, so
# three calls are all it takes: a token, a manifest for the digest and the size, and the blob. The
# transfer writes `<dest>.part` and the next attempt asks for the rest.
#
# NO PYTHON. This was oci_pull.py, and the image that most needs a resumable pull carries no
# interpreter - easyvm's driver is Go and speaks QMP directly. THE ENTIRE DEPENDENCY IS `curl`.
set -u

log() { printf 'oci-pull: %s\n' "$*"; }
fail() { printf '::error::oci-pull: %s\n' "$*" >&2; exit 1; }

tag=${INPUT_TAG:-}
[ -n "$tag" ] || fail "tag is required"
dest_dir=${INPUT_DEST:-out}
registry=${INPUT_REGISTRY:-ghcr.io}
repo=${INPUT_REPOSITORY:-${GITHUB_REPOSITORY:-}}
token=${INPUT_TOKEN:-${GITHUB_TOKEN:-}}
want_path=${INPUT_PATH:-}

[ -n "$repo" ] || fail "repository is required when GITHUB_REPOSITORY is not set"
repo=$(printf '%s' "$repo" | tr '[:upper:]' '[:lower:]')

mkdir -p "$dest_dir"

# --- the three calls ----------------------------------------------------------------------------
if [ -n "$token" ]; then
    scope=$(curl -sS --retry 3 --max-time 120 \
        -u "x:$token" \
        "$(printf 'https://%s/token?scope=repository:%s:pull&service=%s' "$registry" "$repo" "$registry")" \
        2>/dev/null | sed -n 's/.*"token"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1) || scope=""
else
    scope=$(curl -sS --retry 3 --max-time 120 \
        "$(printf 'https://%s/token?scope=repository:%s:pull&service=%s' "$registry" "$repo" "$registry")" \
        2>/dev/null | sed -n 's/.*"token"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1) || scope=""
fi

auth_header=""
[ -n "$scope" ] && auth_header="Authorization: Bearer $scope"

manifest=$(curl -sS --retry 3 --max-time 120 \
    ${auth_header:+-H "$auth_header"} \
    -H "Accept: application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json" \
    "https://$registry/v2/$repo/manifests/$tag" 2>/dev/null) || fail "$tag is not in $registry/$repo"

digest=$(printf '%s' "$manifest" | sed -n 's/.*"digest"[[:space:]]*:[[:space:]]*"\(sha256:[a-f0-9]*\)".*/\1/p' | head -1)
size=$(printf '%s' "$manifest" | sed -n 's/.*"size"[[:space:]]*:[[:space:]]*\([0-9][0-9]*\).*/\1/p' | head -1)
[ -n "$digest" ] || fail "the manifest for $tag has no digest"
[ -n "$size" ] || fail "the manifest for $tag has no size"

# THE LAYER'S OWN TITLE IS THE FILE NAME, and it is read from the manifest rather than guessed: a tag
# can carry more than one file, and the disk is the one with a title.
title=$(printf '%s' "$manifest" | sed -n 's/.*"org.opencontainers.image.title"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)
[ -n "$title" ] || title=$(basename "$tag")

if [ -n "$want_path" ]; then
    dest=$want_path
else
    dest="$dest_dir/$title"
fi
mkdir -p "$(dirname "$dest")"

log "pulling $registry/$repo:$tag -> $dest ($(( size / 1024 / 1024 )) MiB)"
log "layer $digest, $size bytes"

# --- resume -------------------------------------------------------------------------------------
part="$dest.part"

# A FILE THAT ARRIVED IS SKIPPED BY SIZE, NOT BY EXISTENCE. A truncated transfer from an earlier run
# would otherwise look complete forever, and the failure would surface as a corrupt disk much later.
if [ -f "$dest" ]; then
    have=$(wc -c < "$dest" | tr -d ' ')
    if [ "$have" = "$size" ]; then
        log "$dest already complete ($have bytes)"
        printf 'path=%s\nname=%s\ndigest=%s\n' "$dest" "$title" "$digest" >> "${GITHUB_OUTPUT:-/dev/null}"
        exit 0
    fi
    log "$dest is $have bytes but the manifest says $size; discarding it"
    rm -f "$dest"
fi

attempts=${INPUT_ATTEMPTS:-8}
attempt=0
while [ "$attempt" -lt "$attempts" ]; do
    attempt=$((attempt + 1))

    have=0
    [ -f "$part" ] && have=$(wc -c < "$part" | tr -d ' ')
    if [ "$have" = "$size" ]; then
        mv "$part" "$dest"
        log "$dest complete ($size bytes)"
        printf 'path=%s\nname=%s\ndigest=%s\n' "$dest" "$title" "$digest" >> "${GITHUB_OUTPUT:-/dev/null}"
        exit 0
    fi
    if [ "$have" -gt "$size" ]; then
        # A part larger than the blob wrote past the end (a redirect to an error page) and cannot be
        # resumed from.
        log "discarding an oversized part ($have > $size)"
        rm -f "$part"
        have=0
    fi

    log "attempt $attempt/$attempts: $have of $size bytes"
    # `-C -` RESUMES FROM THE .part FILE. The registry answers 206 when it honours `Range`; if it
    # ignores the header curl truncates rather than appending the start to the end.
    if curl -fL --retry 2 --retry-delay 5 --max-time 3600 \
            ${auth_header:+-H "$auth_header"} \
            -C - -o "$part" "https://$registry/v2/$repo/blobs/$digest" 2>/dev/null; then
        got=$(wc -c < "$part" 2>/dev/null | tr -d ' ')
        if [ "${got:-0}" = "$size" ]; then
            mv "$part" "$dest"
            log "$dest complete ($size bytes)"
            printf 'path=%s\nname=%s\ndigest=%s\n' "$dest" "$title" "$digest" >> "${GITHUB_OUTPUT:-/dev/null}"
            exit 0
        fi
        log "got ${got:-0} of $size bytes"
    else
        log "the transfer was cut; the partial file keeps what arrived"
    fi
done

fail "$tag: $attempts attempts and the blob is $(wc -c < "$part" 2>/dev/null || echo 0) of $size bytes.
The downloader resumed from a partial file on every attempt, so this is the registry refusing the blob
rather than resetting the stream."
