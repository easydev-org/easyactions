"""Push a file to an OCI registry as an artifact, with a timestamped tag by default.

    python3 oci_push.py                    # reads the INPUT_* environment

WHY oras AND NOT `docker push`

The payload is a disk image, an ISO or a boot asset - a bare file, not a container image.
oras stores arbitrary files as OCI artifacts without inventing a Dockerfile for them, which
is also what dockur-macos concluded for an 11.8 GiB golden disk.

WHY THE FILE IS PUSHED BY BASENAME FROM ITS OWN DIRECTORY

oras validates the path in a `file:media:type` reference and rejects an absolute one. The
flag it suggests is --disable-path-validation, which turns off ALL of its path checks:
absolute paths, "..", symlinks, and anything outside the working directory. Those checks
exist so a mistyped variable cannot push ~/.ssh/id_rsa to a public package.

A bare filename in the file's own directory satisfies the check instead of disabling it. The
reference is then exactly what it looks like, and symlinks are still refused - which matters
because the caller chooses the work directory.

WHY THIS IS A SCRIPT AND NOT A `run:` BLOCK

The push is the last thing that happens to a multi-gigabyte artifact, and a shell block that
gets a quoting detail wrong fails there - after the expensive part succeeded. Every argument
here is a list element, and the annotation values are built in Python rather than
interpolated by a shell.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

PREFIX = "oci-push"


def log(message: str) -> None:
    print(f"[{PREFIX}] {message}", flush=True)


class Failed(Exception):
    pass


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def resolve(path: str) -> str:
    """The one file `path` names. A glob is allowed, and must match exactly one file.

    AN ARTIFACT'S NAME IS NOT ALWAYS THE CALLER'S TO CHOOSE. android/15/bake.py is told
    `--out out/android15.tar.zst` and its packer renames the result to `out/android15.7z`,
    because 7z cannot write a .tar.zst. The workflow that called it read the name back with

        AVD=$(ls out/android15.* | head -1)

    and an action given a literal path therefore pushed nothing:

        [oci-push] FAILED: no file at out/android15.tar.zst

    A GLOB THAT MATCHES TWO FILES IS AN ERROR, NOT A PICK. `head -1` in the shell silently
    took whichever sorted first, and for a disk that is the difference between publishing
    this build and publishing a stale one beside it.
    """
    import glob as globmod

    if not any(ch in path for ch in "*?["):
        if not os.path.isfile(path):
            raise Failed(f"no file at {path}")
        return path

    matches = sorted(globmod.glob(path))
    matches = [m for m in matches if os.path.isfile(m)]
    if not matches:
        raise Failed(f"{path} matched no file")
    if len(matches) > 1:
        raise Failed(
            f"{path} matched {len(matches)} files; name one of them:\n"
            + "\n".join(f"  {m}" for m in matches[:10])
        )
    log(f"{path} -> {matches[0]}")
    return matches[0]


def sha256_of(path: str) -> str:
    """The file's digest, streamed so a 28 GiB disk does not have to fit in memory."""
    import hashlib

    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def refuse_if_unchanged(path: str, other: str) -> None:
    """Fail when the artifact is byte-identical to the file it was made from.

    A STAGE THAT CHANGES A DISK MUST PRODUCE A DIFFERENT DISK. When it does not, either the
    stage did nothing or its writes went somewhere that was never published - and oras will
    happily reuse the existing content layer and report a successful push either way:

        [oci-push] Exists    f7bd5255e8b0 macos15-desktop.qcow2
        [oci-push] Uploaded  20ce9bcf2e59 macos15-desktop

    That is macos15-desktop after its desktop stage: sixteen walkthrough steps answered, the
    wizard finished, the desktop reached - and the tag resolved to the SAME bytes as
    macos15-setup. The next stage booted it, found the Setup Assistant still on screen, and the
    failure read as "the wizard was never driven".

    IT RUNS BEFORE THE PUSH, because a push that has already reused a layer cannot be undone,
    and because a caller reading the log needs this line rather than a summary three jobs later.
    """
    if not other:
        return
    if not os.path.isfile(other):
        # A MISSING BASELINE IS A CALLER BUG, NOT A REASON TO SKIP THE CHECK. The caller named
        # a file; if it is not there, "distinct from nothing" is not a pass.
        raise Failed(
            f"distinct-from names {other}, which does not exist - so whether this artifact is "
            f"different cannot be answered")
    ours, theirs = sha256_of(path), sha256_of(other)
    if ours == theirs:
        name, base = os.path.basename(path), os.path.basename(other)
        raise Failed(
            f"=== {name} IS BYTE-IDENTICAL TO {base} ===\n"
            f"  sha256 {ours}\n"
            f"  This stage produces a disk, and its output is the disk it was given. Either the\n"
            f"  stage did not run, or it wrote somewhere that is never published (an overlay that\n"
            f"  was not flattened is the usual cause). Publishing it would give every later stage\n"
            f"  a tag that resolves to the stage's INPUT, and they would fail on a disk that\n"
            f"  looks like a wizard nobody drove."
        )
    log(f"  differs from {os.path.basename(other)} ({ours[:12]} vs {theirs[:12]})")


def utc_stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.gmtime())


def oras() -> str:
    """The oras binary, installing it if it is not here.

    IDEMPOTENT, because a job may call this twice (a bake publishes two tags) and a
    download on the second call is a minute spent on something that is already there.
    """
    found = shutil.which("oras")
    if found:
        probe = subprocess.run([found, "version"], capture_output=True, text=True)
        first = probe.stdout.splitlines()[0] if probe.stdout else "unknown version"
        log(f"oras already installed: {first}")
        return found

    machine = platform.machine().lower()
    arch = {"x86_64": "amd64", "amd64": "amd64",
            "aarch64": "arm64", "arm64": "arm64"}.get(machine)
    if arch is None:
        raise Failed(f"no oras build for {machine}")

    version = env("ORAS_VERSION", "1.2.2")
    url = (f"https://github.com/oras-project/oras/releases/download/"
           f"v{version}/oras_{version}_linux_{arch}.tar.gz")
    target = "/usr/local/bin"
    if not os.access(target, os.W_OK):
        target = os.path.join(os.path.expanduser("~"), ".local", "bin")
        os.makedirs(target, exist_ok=True)

    log(f"installing oras {version} from {url}")
    archive = "/tmp/oras.tar.gz"
    # A TRANSIENT 5xx IS NOT A REASON TO FAIL A BAKE.
    #
    # GitHub's release CDN answered 504 twice in one afternoon - freebsd run 41999514167 and
    # nixos derive (cosmic) in 41997535125 - and both bakes had already finished, one of them
    # after an hour. The artifact was built, cleaned and compressed, and the job failed on a
    # download of a 5 MB tool that has nothing to do with it.
    #
    # Four attempts with a growing pause covers the shape of the failure: a CDN edge that is
    # briefly unable to serve and then is. A 404 is NOT retried - that means the URL is wrong
    # and waiting will not fix it - so HTTPError is inspected rather than caught wholesale.
    import time as _time

    last = None
    for attempt in range(4):
        try:
            urllib.request.urlretrieve(url, archive)
            last = None
            break
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code < 500:
                raise Failed(f"could not download oras: {exc}") from exc
        except (urllib.error.URLError, OSError) as exc:
            last = exc                     # connection reset, DNS, timeout: worth retrying
        if attempt < 3:
            pause = 2 ** attempt * 5       # 5s, 10s, 20s
            log(f"  download failed ({last}); retrying in {pause}s")
            _time.sleep(pause)
    if last is not None:
        raise Failed(f"could not download oras after 4 attempts: {last}") from last

    result = subprocess.run(["tar", "-xzf", archive, "-C", target, "oras"],
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise Failed(f"could not extract oras: {(result.stderr or '').strip()[:300]}")
    os.chmod(os.path.join(target, "oras"), 0o755)
    log(f"oras installed at {target}/oras")
    return os.path.join(target, "oras")


def login(registry: str, *, token: str | None = None, user: str | None = None) -> None:
    """Authenticate to the registry for the rest of the job.

    THE TOKEN GOES THROUGH STDIN, NOT ARGV. `oras login -p <token>` puts the credential in
    the process table, where any other process on the runner can read it - and on a shared
    GitHub runner there are several. --password-stdin is the form that does not.
    """
    token = token or env("GITHUB_TOKEN")
    user = user or env("REGISTRY_USER") or "github"
    if not token:
        log("no token to log in with; the push will be anonymous and probably refused")
        return

    log(f"logging in to {registry} as {user}")
    result = subprocess.run(
        ["oras", "login", registry, "--username", user, "--password-stdin"],
        input=token, capture_output=True, text=True,
    )
    for line in (result.stdout or "").splitlines():
        log(f"  {line}")
    if result.returncode != 0:
        raise Failed(
            f"oras login to {registry} failed ({result.returncode}):\n"
            f"{(result.stderr or result.stdout or '').strip()[:600]}"
        )


def push(path: str, reference: str, *, media_type: str,
         annotations: dict[str, str]) -> None:
    """Push one file. `reference` is registry/repo:tag."""
    if not os.path.isfile(path):
        raise Failed(f"no file at {path}")
    if not os.access(path, os.R_OK):
        raise Failed(f"{path} is not readable")

    size = os.path.getsize(path)
    log(f"{path}: {size >> 20} MiB")

    args = ["oras", "push", reference, "--artifact-type", media_type,
            f"{os.path.basename(path)}:{media_type}+zstd"]
    for key, value in annotations.items():
        args += ["--annotation", f"{key}={value}"]

    directory = os.path.dirname(os.path.abspath(path)) or "."
    log(f"pushing -> {reference}")
    result = subprocess.run(args, capture_output=True, text=True, cwd=directory)
    for line in (result.stdout or "").splitlines():
        log(f"  {line}")
    if result.returncode != 0:
        raise Failed(
            f"oras push failed ({result.returncode}):\n"
            f"{(result.stdout or '')[-1200:]}\n{(result.stderr or '')[-1200:]}"
        )
    log(f"published {reference}")


def main() -> int:
    path = env("INPUT_PATH")
    registry = env("INPUT_REGISTRY", "ghcr.io")
    repository = env("INPUT_REPOSITORY") or env("GITHUB_REPOSITORY", "").lower()
    tag = env("INPUT_TAG")
    kind = env("INPUT_KIND")
    stamp = env("INPUT_STAMP", "true").lower() in ("1", "true", "yes")
    media_type = env("INPUT_MEDIA_TYPE", "application/vnd.easydev.disk.qcow2")
    extra = env("INPUT_ANNOTATIONS")  # newline-separated key=value
    distinct_from = env("INPUT_DISTINCT_FROM")

    try:
        if not path:
            raise Failed("path is required")
        path = resolve(path)
        if not repository:
            raise Failed(
                "repository is required: pass it, or run under GitHub Actions where "
                "$GITHUB_REPOSITORY is set"
            )

        # A TAG THAT IS NOT GIVEN IS BUILT FROM THE KIND AND THE CLOCK.
        #
        # "Which build is this" has to stay answerable, and a floating tag would make it
        # not: a disk is not something to redeploy by accident. A caller that wants an
        # exact tag passes one and stamp=false.
        if not tag:
            if not kind:
                raise Failed("either tag or kind is required")
            tag = f"{kind}-{utc_stamp()}" if stamp else kind

        reference = f"{registry}/{repository}:{tag}"

        annotations = {
            "org.opencontainers.image.source":
                f"{env('GITHUB_SERVER_URL', 'https://github.com')}/{repository}",
            "org.opencontainers.image.revision": env("GITHUB_SHA", "local"),
            "org.opencontainers.image.created":
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        if kind:
            annotations["org.opencontainers.image.title"] = kind
            annotations["dev.easydev.kind"] = kind
        for line in extra.splitlines():
            line = line.strip()
            if not line or "=" not in line:
                continue
            key, _, value = line.partition("=")
            annotations[key.strip()] = value.strip()

        # BEFORE the push: a layer oras already has is a truth that cannot be taken back.
        refuse_if_unchanged(path, distinct_from)

        oras()
        login(registry)
        push(path, reference, media_type=media_type, annotations=annotations)

        # The tag is what every downstream job needs, so it goes to GITHUB_OUTPUT - the
        # runner's own mechanism, not a third party's.
        out = os.environ.get("GITHUB_OUTPUT")
        if out:
            with open(out, "a", encoding="utf-8") as fh:
                fh.write(f"tag={tag}\n")
                fh.write(f"reference={reference}\n")
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a", encoding="utf-8") as fh:
                fh.write(f"## Published\n\n```\n{reference}\n```\n\n")
                if kind:
                    fh.write(f"- kind: `{kind}`\n")
        return 0
    except Failed as exc:
        print(f"\n[{PREFIX}] FAILED: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
