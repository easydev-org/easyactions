# oci-pull

Pull an OCI artifact, **resuming the blob across resets**.

```yaml
- name: pull the setup disk
  id: pull
  uses: easydev-org/easyactions/oci-pull@main
  with:
    tag: macos15-setup
    dest: out
```

## Why not `oras pull`

`oras pull` has no resume. A transfer reset at the other end starts again from byte zero, and a
28 GiB blob over a long-lived HTTP/2 stream resets often enough that five attempts can all fail
without making net progress:

```
attempt 1  03:39:28 -> 03:45:00   stream error: stream ID 1; PROTOCOL_ERROR
attempt 2  03:45:05 -> 03:48:58   the same
attempt 3  03:49:08 -> 03:55:03   the same
```

Three attempts, five to six minutes each, and the file never got past the first.

So this makes the three calls a pull needs - manifest, token, blob - and streams the blob with
`Range: bytes=<have>-` on every attempt after the first.

**Measured on a real 191 MiB artifact:**

```
attempt 1/5, 0 of 191758336 bytes
attempt 1 failed: [SSL: RECORD_LAYER_FAILURE] record layer failure
attempt 2/5, 155189248 of 191758336 bytes      <- resumed, not restarted
redox-desktop.qcow2 is 191758336 bytes
```

## The credential exchange is basic auth, not bearer

ghcr's token endpoint takes the credential as `username:password`. Sending the same token as
`Authorization: Bearer` answers **403 `invalid token`** - which reads as "the artifact is not
there" and is not:

```
Authorization: Bearer <github-token>   -> 403 invalid token
username:token basic auth              -> 200, a scoped pull token
that scoped token on /v2/manifests     -> 200
```

A **public** package hides the mistake: its blobs serve anonymously, so pulling from one works
with no credential at all. The first private package is where the broken exchange shows up.

## A file that exists is not a file that arrived

The manifest states the layer's size. A destination at that size is complete; one at any other
size is a truncated transfer from an earlier attempt and is discarded rather than resumed -
resuming onto a file whose length is a guess is how a 10 GiB disk reaches qemu with a hole in
it. The `.part` file is what carries a partial transfer between attempts.

## Outputs

`path`, `name` and `digest`.
