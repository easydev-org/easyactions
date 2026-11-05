"""Pull an OCI artifact from a registry, resuming across resets.

    python3 oci_pull.py                    # reads the INPUT_* environment

WHY NOT `oras pull`, WHICH IS WHAT THIS USED TO BE

`oras pull` has no resume. A transfer that is reset at the other end starts again from byte
zero, and a 28 GiB blob over a long-lived HTTP/2 stream is reset often enough that five
attempts can all fail without making net progress:

    attempt 1  03:39:28 -> 03:45:00   stream error: stream ID 1; PROTOCOL_ERROR
    attempt 2  03:45:05 -> 03:48:58   the same
    attempt 3  03:49:08 -> 03:55:03   the same
    attempt 4  03:55:18 -> ...        the same

Three attempts, five to six minutes each, and the file never got further than the first.

So this speaks the three calls a pull actually needs - manifest, token, blob - and streams the
blob itself:

    GET /v2/<repo>/manifests/<tag>     Accept: application/vnd.oci.image.manifest.v1+json
    GET /token?scope=repository:<repo>:pull&service=<host>
    GET /v2/<repo>/blobs/<digest>      Range: bytes=<have>-

`oras` is still what PUBLISHES. It is only the pull that needed a different mechanism.

WHAT IT DOES NOT DO

It does not fall back to another tag or registry. The caller names one artifact, and "the pull
failed" is a fact worth reporting rather than hiding behind a mirror that may hold a different
build.

A FILE THAT EXISTS IS NOT A FILE THAT ARRIVED

The manifest states the layer's size. A destination that exists at that size is complete; one
at any other size is a truncated transfer from an earlier attempt and is discarded, not
resumed - resuming onto a file whose length is a guess is how a 10 GiB disk gets handed to qemu
with a hole in it. The `.part` file is what carries a partial transfer between attempts, and
its length is the one the server was told to continue from.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request

PREFIX = "oci-pull"

# A TEN GIGABYTE TRANSFER OVER THE PUBLIC INTERNET DOES NOT SUCCEED ONCE.
#
# Run 41481670715 died pulling a golden disk:
#
#     size mismatch: got 3472883712, the manifest says 11286607360
#
# 3.2 GiB of 10.5 GiB, and nothing was retried. Five attempts, each resuming from what the
# .part file already holds.
ATTEMPTS = 5


def log(message: str) -> None:
    print(f"[{PREFIX}] {message}", flush=True)


class Failed(Exception):
    pass


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


class Registry:
    def __init__(self, host: str, repo: str, *, token: str | None = None):
        self.host = host
        self.repo = repo
        self.token = token if token is not None else env("GITHUB_TOKEN")
        self.scheme = env("OCI_SCHEME", "https")

    def _auth_headers(self) -> dict[str, str]:
        """The headers a blob or manifest request needs.

        `self.token` holds a REGISTRY-SCOPED token by the time this is called - see
        _pull_token(). A GitHub token is not one: ghcr answers

            {"errors":[{"code":"DENIED","message":"invalid token"}]}

        with 403 to a Bearer GitHub token on /v2/, which reads as "the artifact is not
        there" and is not.
        """
        if not self.token:
            return {}
        return {"Authorization": "Bearer " + self.token}

    def _pull_token(self) -> str | None:
        """A registry-scoped pull token, if this registry issues one.

        TWO THINGS HAVE TO BE RIGHT, AND BOTH WERE WRONG IN THE FIRST VERSION.

        THE EXCHANGE IS BASIC AUTH, NOT BEARER. ghcr's token endpoint takes the GitHub
        credential as `username:password`; sending the same token as
        `Authorization: Bearer ...` answers 200 with no token in it, or 403, depending on
        the package's visibility. Measured against a private package:

            Authorization: Bearer <github-token>   -> 403 invalid token
            username:token basic auth              -> 200, a scoped token
            (that scoped token on /v2/manifests)   -> 200

        A PUBLIC PACKAGE HIDES THE MISTAKE. easydiskbake's artifacts serve anonymously, so a
        pull from there worked with no credential at all; the first private package is where
        the broken exchange shows up.

        THE USERNAME IS THE TOKEN'S OWNER. For a workflow token that is `github` (or
        `x-access-token`, which ghcr also accepts); for a personal token it is the login. It
        is an input rather than a guess, defaulting to what GITHUB_ACTOR says in a workflow.
        """
        url = (f"{self.scheme}://{self.host}/token?scope=repository:{self.repo}:pull"
               f"&service={self.host}")
        req = urllib.request.Request(url)
        if self.token:
            user = env("REGISTRY_USER") or env("GITHUB_ACTOR") or "github"
            raw = f"{user}:{self.token}".encode()
            req.add_header("Authorization",
                           "Basic " + base64.b64encode(raw).decode())
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.load(resp)["token"]
        except (urllib.error.URLError, KeyError, ValueError) as exc:
            # A registry without a /token endpoint (or one that answers 404 for it) is not
            # necessarily a registry that refuses the request - an anonymous pull of a
            # public package needs no token at all.
            log(f"  no scoped token available ({exc}); trying without one")
            return None

    def manifest(self, tag: str) -> dict:
        url = f"{self.scheme}://{self.host}/v2/{self.repo}/manifests/{tag}"
        req = urllib.request.Request(url)
        for key, value in self._auth_headers().items():
            req.add_header(key, value)
        req.add_header("Accept", "application/vnd.oci.image.manifest.v1+json")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403, 404):
                raise Failed(
                    f"no manifest for tag {tag!r} in {self.host}/{self.repo}\n"
                    f"run the publishing workflow for that name; this will not fall back "
                    f"to a mirror, because a mirror can hold a different build."
                ) from exc
            raise

    def blob(self, digest: str, dest: str, *, expected: int) -> None:
        """Stream one blob to dest, resuming from dest.part across attempts.

        The partial file is what makes a failure resumable rather than restarting a
        multi-gigabyte transfer: a half-written dest would be discarded by the size check
        at the top, so the bytes already in flight are kept in .part and the next attempt
        asks for the rest with a Range header.
        """
        part = dest + ".part"
        url = f"{self.scheme}://{self.host}/v2/{self.repo}/blobs/{digest}"

        for attempt in range(1, ATTEMPTS + 1):
            have = os.path.getsize(part) if os.path.exists(part) else 0
            if have >= expected:
                # A part that already holds the whole blob: a previous attempt finished the
                # transfer and stopped before the rename.
                os.replace(part, dest)
                log(f"  {dest} completed from an earlier attempt")
                return

            log(f"  attempt {attempt}/{ATTEMPTS}, {have} of {expected} bytes")
            req = urllib.request.Request(url)
            for key, value in self._auth_headers().items():
                req.add_header(key, value)
            if have:
                req.add_header("Range", f"bytes={have}-")

            try:
                with urllib.request.urlopen(req, timeout=300) as resp:
                    # 206 means the Range was honoured; 200 means it was not, and the body
                    # starts at zero - which would append the whole blob onto the part.
                    status = getattr(resp, "status", 200)
                    mode = "ab" if (have and status == 206) else "wb"
                    if have and status != 206:
                        log("  the registry ignored the Range; starting over")
                        have = 0
                    with open(part, mode) as fh:
                        while True:
                            chunk = resp.read(1 << 20)
                            if not chunk:
                                break
                            fh.write(chunk)
            except (urllib.error.URLError, OSError, TimeoutError) as exc:
                log(f"  attempt {attempt} failed: {exc}")
                if attempt == ATTEMPTS:
                    raise Failed(
                        f"could not download {digest} in {ATTEMPTS} attempts: {exc}\n"
                        f"  each attempt resumed from the .part file, so this is the\n"
                        f"  registry refusing the blob rather than the stream resetting."
                    ) from exc
                time.sleep(2 * attempt)
                continue

            got = os.path.getsize(part)
            if got == expected:
                os.replace(part, dest)
                log(f"  {dest} is {got} bytes")
                return
            if got > expected:
                os.unlink(part)
                raise Failed(
                    f"{part} is {got} bytes but the manifest says {expected}; the "
                    f"registry served more than the manifest declares"
                )
            log(f"  attempt {attempt} stopped at {got} of {expected} bytes")

        part_size = os.path.getsize(part) if os.path.exists(part) else 0
        raise Failed(
            f"could not download {digest}: made no progress past {part_size} of "
            f"{expected} bytes in {ATTEMPTS} attempts\n"
            f"  every attempt resumed from the partial file, so this is the registry\n"
            f"  refusing the blob rather than the stream resetting."
        )


def main() -> int:
    tag = env("INPUT_TAG")
    dest = env("INPUT_DEST", "out")
    host = env("INPUT_REGISTRY", "ghcr.io")
    repository = env("INPUT_REPOSITORY") or env("GITHUB_REPOSITORY", "").lower()
    want = env("INPUT_PATH")     # optional: the file name to place it under
    token = env("GITHUB_TOKEN") or env("OCI_TOKEN")

    try:
        if not tag:
            raise Failed("tag is required")
        if not repository:
            raise Failed(
                "repository is required: pass it, or run under GitHub Actions where "
                "$GITHUB_REPOSITORY is set"
            )

        reg = Registry(host, repository.lower(), token=token or None)
        scoped = reg._pull_token()
        if scoped:
            # The scoped token is the one a blob request is authorised by; the GitHub token
            # is not accepted on the /v2/ endpoints of a private package.
            reg.token = scoped

        log(f"pulling {host}/{repository}:{tag}")
        manifest = reg.manifest(tag)
        layers = manifest.get("layers") or []
        if not layers:
            raise Failed(f"manifest for {tag!r} has no layers")

        layer = layers[0]
        digest = layer["digest"]
        size = layer["size"]
        log(f"layer {digest}, {size} bytes")

        # THE DESTINATION NAME COMES FROM THE MANIFEST WHEN IT CAN.
        #
        # The annotation the publisher set carries the file's real name; a caller that
        # knows better can override it with `path`. Falling back to <tag>.qcow2 keeps the
        # old behaviour for artifacts published before the annotation existed.
        annotations = manifest.get("annotations") or {}
        name = want or annotations.get("org.opencontainers.image.title-name") or f"{tag}.qcow2"
        os.makedirs(dest, exist_ok=True)
        target = os.path.join(dest, os.path.basename(name))

        if os.path.exists(target):
            have = os.path.getsize(target)
            if have == size:
                log(f"{target} already present ({have} bytes)")
                out = os.environ.get("GITHUB_OUTPUT")
                if out:
                    with open(out, "a", encoding="utf-8") as fh:
                        fh.write(f"path={target}\n")
                print(target)
                return 0
            log(f"{target} is {have} bytes but the manifest says {size}; discarding it")
            os.unlink(target)

        reg.blob(digest, target, expected=size)

        out = os.environ.get("GITHUB_OUTPUT")
        if out:
            with open(out, "a", encoding="utf-8") as fh:
                fh.write(f"path={target}\n")
                fh.write(f"name={os.path.basename(target)}\n")
                fh.write(f"digest={digest}\n")
        print(target)
        return 0
    except Failed as exc:
        print(f"\n[{PREFIX}] FAILED: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
