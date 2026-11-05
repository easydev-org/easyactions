"""Assert /dev/kvm and free disk before a VM job starts.

    python3 runner_check.py          # reads INPUT_KVM / INPUT_DISK_GB / INPUT_PRUNE

WHY THIS IS AN ACTION AND NOT FOUR LINES OF YAML IN EVERY WORKFLOW

`python3 lib/ci.py kvm` and `python3 lib/ci.py free-disk --need N` appear in twenty-six
places across the workflows of one repository. Two things went wrong with that, both of
them the same fault wearing different clothes:

    the same shell was copied into fourteen prepares, so a fix had to be made fourteen
    times and one of them was always missed

    the number was chosen per copy, so launch (cosmic) asked for 40 GB to run a 3769 MiB
    disk and failed on a runner that had 11 - next to twelve jobs that passed on the same
    image

An action gives both one home. `disk-gb` is still per stage, because the right number really
does differ; what it is not is per COPY.

WHAT EACH CHECK IS FOR

    kvm       a bake needs /dev/kvm, and there is no software fallback worth pretending
              about: qemu's TCG runs a Windows install in most of a day, past every timeout
              these workflows set. Failing here says so in one line.

    disk      a disk, its working copy, its compressed result and a recording do not fit in
              the default runner's free space. Failing here says how much is there.

FAILURE IS NON-ZERO, AND THE MESSAGE IS THE POINT. A step that dies with `exit 1` after a
minute of silence costs a log dig; the messages below name the runner, the number, and the
likely cause.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys

# The same log formatting the workflows use, so a failure is greppable in one shape.
PREFIX = "runner"


def log(message: str) -> None:
    print(f"[{PREFIX}] {message}", flush=True)


class Failed(Exception):
    pass


# --- /dev/kvm -----------------------------------------------------------------


def check_kvm() -> None:
    """Assert that this process can open /dev/kvm read-write.

    THE CHECK IS AN OPEN, NOT os.access.

    /dev/kvm is 0o660 and owned by a group, and on a GitHub runner the job's uid is not the
    owner - so os.access() reports no write permission even when the process IS in that
    group. A version of this check that used os.access failed every bake on a runner where
    KVM worked perfectly:

        /dev/kvm: mode 0o660, char device 10:232
        FAILED: /dev/kvm is not readable and writable by uid 1001

    Opening it is the only test that reflects what qemu will do with it.
    """
    path = "/dev/kvm"
    if not os.path.exists(path):
        raise Failed(
            f"{path} does not exist on this runner.\n"
            f"A job that boots a machine cannot run without it, and there is no\n"
            f"software fallback that fits these timeouts. On a container job, pass\n"
            f"`options: --device /dev/kvm`."
        )

    info = os.stat(path)
    log(f"{path}: mode {oct(info.st_mode & 0o777)}, "
        f"char device {os.major(info.st_rdev)}:{os.minor(info.st_rdev)}")

    try:
        with open(path, "r+b"):
            pass
    except OSError as exc:
        # The node exists but this uid cannot use it. On a GitHub runner it is 0o660
        # root:kvm and the job's uid is neither.
        #
        # chmod 0666 is safe here and is the standard fix: /dev/kvm is a character device
        # that grants only the ability to ask the kernel for a VM it is already willing to
        # run, and a job that has reached this code can already run qemu. Refusing instead
        # makes every bake on a stock runner fail in prepare.
        log(f"{path} is not accessible to uid {os.getuid()} ({exc}); granting 0666")
        result = None
        if os.access(path, os.W_OK):
            # A container job runs as root and the device arrives via --device, so no
            # escalation is needed and sudo may not even be installed.
            os.chmod(path, 0o666)
        elif shutil.which("sudo"):
            result = subprocess.run(
                ["sudo", "chmod", "0666", path], capture_output=True, text=True,
            )
        else:
            raise Failed(
                f"{path} cannot be opened by uid {os.getuid()}, and neither a direct\n"
                f"chmod nor sudo is available. The runner has to expose {path}; on a\n"
                f"container job that is `options: --device /dev/kvm`."
            ) from exc

        if result is not None and result.returncode != 0:
            raise Failed(
                f"{path} cannot be opened by uid {os.getuid()} and could not be made "
                f"accessible:\n{(result.stderr or '').strip()[:300]}\n"
                f"The runner has to expose {path}; on a container job that is\n"
                f"`options: --device /dev/kvm`."
            ) from exc

        try:
            with open(path, "r+b"):
                pass
        except OSError as second:
            raise Failed(
                f"{path} is still not accessible after chmod 0666: {second}"
            ) from second

    log("kvm: usable")


# --- disk ---------------------------------------------------------------------


def available_gb() -> int:
    """Free space on / in whole GB."""
    return shutil.disk_usage("/").free // (1024 ** 3)


def free_what_is_safe() -> None:
    """Delete what nothing on this runner needs, with a timeout on each.

    ORDERED BY WHAT ACTUALLY HOLDS SPACE. Docker layers come first: a job that ran in this
    VM left its image behind, and an image is tens of gigabytes where a toolchain directory
    is a few.

    THE PATHS THAT ARE NOT TOUCHED, WHICH IS WHY THIS LIST IS SHORT

        /opt/hostedtoolcache            Actions reads it to resolve tool versions
        /usr/local/lib/node_modules     the runner's own JavaScript actions live here

    Deleting either makes a job fail two steps later with an error that has nothing to do
    with disk space, which is a worse trade than failing here.

    A PRUNE THAT HANGS MUST NOT HOLD THE JOB. `docker system prune` talks to a daemon and
    has been measured at 984 seconds doing nothing; every call has a timeout and a failure
    is logged and skipped.
    """
    for args, why in (
        (["docker", "system", "prune", "-af"], "images, containers and build cache"),
        (["docker", "builder", "prune", "-af"], "the build cache alone"),
    ):
        log(f"  docker prune ({why})")
        try:
            result = subprocess.run(args, capture_output=True, text=True, timeout=180)
            if result.returncode != 0:
                log(f"    skipped: {(result.stderr or '').strip()[:120]}")
        except (OSError, subprocess.SubprocessError) as exc:
            log(f"    skipped: {exc}")

    for path, what in (
        ("/usr/share/dotnet", "the hosted .NET SDK"),
        ("/usr/local/lib/android", "the hosted Android SDK"),
        ("/opt/ghc", "the hosted GHC toolchain"),
        ("/opt/hostedtoolcache/CodeQL", "the CodeQL bundle"),
    ):
        if not os.path.exists(path):
            continue
        log(f"  removing {path} ({what})")
        try:
            subprocess.run(["rm", "-rf", path], timeout=120, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            log(f"    skipped: {exc}")


def check_disk(need_gb: int, *, prune: bool) -> None:
    """Ensure `need_gb` is free, freeing what is safe to free when it is not.

    THE MEASUREMENT THAT DECIDED THIS ORDER

    It used to refuse outright, on the measurement that the runner had 121 GB free against
    a 30 GB requirement and that deleting anything was noise. That was true of the runner
    it was measured on and false of the one that failed:

        macOS bake prepare       available: 81 GB (need 80)     <- it fit, barely
        freebsd launch prepare   available:  9 GB (need 30)     <- it did not

    Both declare the same runner image. The difference is what that VM happened to carry
    and what an earlier job on it left behind, and a launch stage is the wrong place to
    discover it - the run dies in its prepare step before its own code runs a line.

    So this frees first and refuses second. The order matters because the check's job is to
    guarantee space, not to report a number.
    """
    if need_gb <= 0:
        log("disk: not checked (disk-gb is 0)")
        return

    available = available_gb()
    log(f"available: {available} GB (need {need_gb})")
    if available >= need_gb:
        return

    if not prune:
        raise Failed(
            f"at least {need_gb} GB is required, {available} GB is free, and prune is off.\n"
            f"`df -h /` on the runner says what occupies it."
        )

    log(f"  {available} GB is not enough; freeing what can be freed")
    free_what_is_safe()
    available = available_gb()
    log(f"  after freeing: {available} GB")

    if available >= need_gb:
        log("  enough now")
        return

    raise Failed(
        f"at least {need_gb} GB is required, {available} GB is free even after freeing\n"
        f"docker images, the .NET and Android toolchains, and the build cache.\n"
        f"This runner is smaller than the job needs; use a larger one.\n"
        f"`df -h /` on the runner says what actually occupies it."
    )


# --- main ---------------------------------------------------------------------


def main() -> int:
    want_kvm = os.environ.get("INPUT_KVM", "true").strip().lower() in ("1", "true", "yes")
    try:
        need_gb = int(os.environ.get("INPUT_DISK_GB", "0").strip() or "0")
    except ValueError:
        log(f"disk-gb must be an integer, not "
            f"{os.environ.get('INPUT_DISK_GB')!r}")
        return 2
    prune = os.environ.get("INPUT_PRUNE", "true").strip().lower() in ("1", "true", "yes")

    log(f"{platform.system()} {platform.machine()}, "
        f"uid {os.getuid()}, kvm={want_kvm}, disk={need_gb} GB")

    try:
        if want_kvm:
            check_kvm()
        else:
            log("kvm: not required by this job")
        check_disk(need_gb, prune=prune)
    except Failed as exc:
        print(f"\n[{PREFIX}] FAILED: {exc}", file=sys.stderr, flush=True)
        return 1
    log("ready")
    return 0


if __name__ == "__main__":
    sys.exit(main())
