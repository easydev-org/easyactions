#!/bin/sh
# ASSERT THE TWO THINGS EVERY VM JOB NEEDS BEFORE IT STARTS.
#
#	/dev/kvm that this process can ACTUALLY OPEN, and enough free disk - pruning what is safe to
#	prune when it is short. Fails with a message that names the fix, rather than a timeout twenty
#	minutes later.
#
# WHY THE KVM CHECK IS AN OPEN AND NOT A PERMISSION TEST. /dev/kvm is 0o660 and owned by a group, and
# on a GitHub runner the job's uid is not the owner - so `test -w /dev/kvm` reports no permission even
# when the process IS in that group. A version of this check that used `os.access` (the Python one)
# failed every bake on a runner where KVM worked perfectly:
#
#	/dev/kvm: mode 0o660, char device 10:232
#	FAILED: /dev/kvm is not readable and writable by uid 1001
#
# OPENING IT IS THE ONLY TEST THAT REFLECTS WHAT QEMU WILL DO WITH IT:
#
#	( exec 9<>/dev/kvm )    a read-write open, in a subshell so the fd does not leak
set -u

log() { printf 'runner-check: %s\n' "$*"; }
fail() { printf '::error::runner-check: %s\n' "$*" >&2; exit 1; }

kvm=${INPUT_KVM:-true}
disk_gb=${INPUT_DISK_GB:-0}
prune=${INPUT_PRUNE:-true}
name=${INPUT_CHECK_NAME:-runner-check}

# --- /dev/kvm -----------------------------------------------------------------------------------
if [ "$kvm" = "true" ]; then
    [ -e /dev/kvm ] || fail "/dev/kvm does not exist on this runner. A job that boots a machine
cannot run without it, and there is no software fallback that fits these timeouts. On a container
job, pass \`options: --device /dev/kvm\`."

    # THE MODE AND THE MAJOR:MINOR ARE LOGGED, because "it exists" and "it works" are different
    # questions and the numbers are what a person needs when the answer is the second one.
    log "$(ls -l /dev/kvm | sed 's/  */ /g')"

    if ! ( exec 9<>/dev/kvm ) 2>/dev/null; then
        # chmod 0666 IS SAFE AND IS THE STANDARD FIX. /dev/kvm is a character device that grants only
        # the ability to ask the kernel for a VM it is already willing to run, and a job that has
        # reached this code can already run qemu. Refusing instead makes every bake on a stock runner
        # fail in prepare.
        log "/dev/kvm is not accessible to uid $(id -u); granting 0666"
        if [ -w /dev/kvm ] || [ "$(id -u)" = "0" ]; then
            chmod 0666 /dev/kvm 2>/dev/null || true
        elif command -v sudo >/dev/null 2>&1; then
            sudo chmod 0666 /dev/kvm 2>/dev/null || true
        fi
        ( exec 9<>/dev/kvm ) 2>/dev/null || fail "/dev/kvm exists but cannot be opened by uid
$(id -u), and chmod 0666 did not help. The runner has to expose it: on a container job, pass
\`options: --device /dev/kvm\`."
    fi
    log "/dev/kvm opens read-write"
fi

# --- disk ---------------------------------------------------------------------------------------
#
# THE WORKING DIRECTORY IS THE ONE THAT HAS TO HOLD THE WORK, not `/`. A runner job gets its own
# filesystem under $GITHUB_WORKSPACE, and `/` can be roomy while that mount is full.
[ "$disk_gb" = "0" ] && exit 0

need_bytes=$(( disk_gb * 1024 * 1024 * 1024 ))
target=${GITHUB_WORKSPACE:-.}
avail=$(df -PB1 "$target" 2>/dev/null | awk 'NR==2 {print $4}')
[ -n "${avail:-}" ] || { log "cannot read free space for $target; skipping the disk check"; exit 0; }

log "$target has $(( avail / 1024 / 1024 / 1024 )) GiB free; $disk_gb GiB needed"

if [ "$avail" -ge "$need_bytes" ]; then
    exit 0
fi

# PRUNE WHAT A VM JOB NEVER USES. The runner images carry the .NET SDK, the Android SDK, GHC and
# CodeQL whether or not a job uses them - measured at 20 to 30 GiB between them.
if [ "$prune" = "true" ]; then
    for d in \
        /usr/share/dotnet \
        /usr/local/lib/android \
        /opt/ghc \
        /usr/local/.ghcup \
        /opt/hostedtoolcache/CodeQL \
        /usr/local/share/boost
    do
        [ -d "$d" ] || continue
        before=$(df -PB1 "$target" | awk 'NR==2 {print $4}')
        chmod -R u+w "$d" 2>/dev/null || true
        rm -rf "$d" 2>/dev/null || true
        after=$(df -PB1 "$target" | awk 'NR==2 {print $4}')
        log "pruned $d: +$(( (after - before) / 1024 / 1024 )) MiB"
    done
    avail=$(df -PB1 "$target" 2>/dev/null | awk 'NR==2 {print $4}')
fi

[ "${avail:-0}" -ge "$need_bytes" ] || fail "$target has $(( avail / 1024 / 1024 / 1024 )) GiB free
but this job needs $disk_gb GiB. Pruning did not free enough: the disk's virtual size is not the
same as what it writes, and the recording and the compressed result are both on this mount."
exit 0
