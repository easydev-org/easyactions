#!/bin/sh
# PUSH A FILE TO A REGISTRY AS AN OCI ARTIFACT.
#
# THIS IS A WRAPPER AROUND `oras push`, AND SO WAS oci_push.py. What the wrapper adds is the four
# decisions oras leaves to a caller, and each of them was paid for by a run:
#
#	INSTALLING oras            idempotently, because a job may publish two tags
#	SHA256 AND --distinct-*    refuse to push bytes that are already published
#	--stamp                    a tag nothing has to invent twice
#	LOGIN WITH THE TOKEN       explicitly, because GITHUB_TOKEN in the environment is not enough
#
# NO PYTHON. The image that publishes these disks carries no interpreter - easyvm's driver is Go and
# speaks QMP directly - and `oras` is the tool that knows the registry protocol. The only other
# dependency is `sha256sum`, which coreutils provides.
set -u

log() { printf 'oci-push: %s\n' "$*"; }
fail() { printf '::error::oci-push: %s\n' "$*" >&2; exit 1; }

path=${INPUT_PATH:-}
[ -n "$path" ] || fail "path is required"
[ -f "$path" ] || fail "$path does not exist"
size=$(wc -c < "$path" | tr -d ' ')
[ "$size" -gt 0 ] || fail "$path is empty; an artifact of no bytes is not an artifact"

tag=${INPUT_TAG:-}
kind=${INPUT_KIND:-}
stamp=${INPUT_STAMP:-true}
registry=${INPUT_REGISTRY:-ghcr.io}
repo=${INPUT_REPOSITORY:-${GITHUB_REPOSITORY:-}}
media_type=${INPUT_MEDIA_TYPE:-application/vnd.easydev.artifact}
annotations=${INPUT_ANNOTATIONS:-}
token=${INPUT_TOKEN:-${GITHUB_TOKEN:-}}
distinct_from=${INPUT_DISTINCT_FROM:-}
distinct_digest=${INPUT_DISTINCT_DIGEST:-}
check_name=${INPUT_CHECK_NAME:-oci-push}

[ -n "$repo" ] || fail "repository is required when GITHUB_REPOSITORY is not set"
repo=$(printf '%s' "$repo" | tr '[:upper:]' '[:lower:]')

# --- a tag ---------------------------------------------------------------------------------------
if [ -z "$tag" ]; then
    [ -n "$kind" ] || fail "either tag or kind is required"
    tag=$kind
    # `stamp: true` APPENDS THE UTC TIME, because a tag that two runs both compute is a tag they
    # overwrite - which is what makes a published artifact unreproducible.
    if [ "$stamp" = "true" ]; then
        tag="$kind-$(date -u +%Y%m%d-%H%M%S)"
    fi
fi

# --- refuse to push what is already published -----------------------------------------------------
#
# A PUSH OF IDENTICAL BYTES COSTS A MINUTE AND CHANGES NOTHING, and when the caller says these bytes
# must differ from an earlier artifact, publishing them anyway hides a real failure.
ours=$(sha256sum "$path" | awk '{print $1}')

if [ -n "$distinct_digest" ]; then
    cleaned=$(printf '%s' "$distinct_digest" | sed 's/^sha256://')
    case "$cleaned" in
        *[!0-9a-f]*) fail "distinct-digest is $distinct_digest, which is not a sha256" ;;
    esac
    [ "${#cleaned}" -eq 64 ] || fail "distinct-digest is $distinct_digest, which is not a sha256"
    [ "$cleaned" != "$ours" ] || fail "$path is sha256 $ours, which is the digest this caller said must
NOT be published - the work produced identical bytes, so something upstream did not run"
fi

if [ -n "$distinct_from" ]; then
    [ -f "$distinct_from" ] || fail "distinct-from $distinct_from does not exist"
    other=$(sha256sum "$distinct_from" | awk '{print $1}')
    [ "$other" != "$ours" ] || fail "$path and $distinct_from are byte-identical (sha256 $ours).
The stage was supposed to change the disk and did not"
fi

# --- oras ----------------------------------------------------------------------------------------
if command -v oras >/dev/null 2>&1; then
    log "oras already installed: $(oras version 2>/dev/null | head -1)"
else
    log "installing oras"
    machine=$(uname -m)
    case "$machine" in
        x86_64|amd64) arch=amd64 ;;
        aarch64|arm64) arch=arm64 ;;
        *) fail "no oras build for $machine" ;;
    esac
    version=1.2.2
    url="https://github.com/oras-project/oras/releases/download/v$version/oras_${version}_linux_${arch}.tar.gz"
    tmp=$(mktemp -d)
    curl -fsSL "$url" -o "$tmp/oras.tgz" || fail "could not download oras from $url"
    tar -xzf "$tmp/oras.tgz" -C "$tmp" oras || fail "could not unpack oras"
    install -m 0755 "$tmp/oras" /usr/local/bin/oras 2>/dev/null || {
        mkdir -p "$HOME/.local/bin"
        install -m 0755 "$tmp/oras" "$HOME/.local/bin/oras"
        PATH="$HOME/.local/bin:$PATH"
        export PATH
    }
    rm -rf "$tmp"
    command -v oras >/dev/null 2>&1 || fail "oras was unpacked and is still not on PATH"
fi

# EXPLICIT LOGIN, BECAUSE AN ENVIRONMENT VARIABLE IS NOT ENOUGH. oras reads no `GITHUB_TOKEN`; without
# this the push is anonymous and the registry answers `DENIED`, which reads like a permission problem
# on the package.
if [ -n "$token" ]; then
    printf '%s' "$token" | oras login "$registry" -u "${GITHUB_ACTOR:-x}" --password-stdin \
        >/dev/null 2>&1 || fail "oras login to $registry failed"
    log "logged in to $registry"
fi

# --- the push ------------------------------------------------------------------------------------
#
# ORAS PUSHES FROM THE FILE'S OWN DIRECTORY WITH A BARE FILENAME: it validates the path in a
# `file:media:type` reference and rejects an absolute one, and the flag it suggests
# (`--disable-path-validation`) turns off ALL of its path checks - absolute paths, "..", symlinks.
# Those exist so a mistyped variable cannot publish ~/.ssh/id_rsa, so a bare name in its own
# directory satisfies the check instead.
reference="$registry/$repo:$tag"
dir=$(dirname "$path")
name=$(basename "$path")

log "pushing $reference ($(( size / 1024 / 1024 )) MiB)"

set -- push "$reference" \
    --artifact-type "$media_type" \
    "$name:application/vnd.oci.image.layer.v1.tar" \
    --annotation "org.opencontainers.image.source=${GITHUB_SERVER_URL:-https://github.com}/$repo" \
    --annotation "org.opencontainers.image.revision=${GITHUB_SHA:-unknown}"

if [ -n "$annotations" ]; then
    old_ifs=$IFS
    IFS='
'
    for one in $annotations; do
        [ -n "$one" ] || continue
        set -- "$@" --annotation "$one"
    done
    IFS=$old_ifs
fi

if ! ( cd "$dir" && oras "$@" ); then
    fail "oras push $reference failed. The disk is $(( size / 1024 / 1024 )) MiB and it is intact on
this runner; the recording and the console log are the evidence the workflow uploads next."
fi

log "pushed $reference"

# --- what a later step reads ---------------------------------------------------------------------
printf 'tag=%s\ndigest=sha256:%s\nreference=%s\nsize=%s\n' \
    "$tag" "$ours" "$reference" "$size" >> "${GITHUB_OUTPUT:-/dev/null}"

# THE DIGEST IS ALSO PRINTED, because a caller that compares two pushes needs the number and the
# step's log is where a person looks for it.
log "sha256 $ours"
