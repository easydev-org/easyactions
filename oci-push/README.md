# oci-push

Push a file to a container registry as an OCI artifact.

```yaml
- name: publish
  id: publish
  uses: easydev-org/easyactions/oci-push@main
  with:
    path: out/macos15-desktop.qcow2
    kind: macos15-desktop          # tag becomes macos15-desktop-20261105-032959
    media-type: application/vnd.easydev.disk.qcow2
```

## Why oras and not `docker push`

The payload is a disk image, an ISO or a boot asset - a file, not a container image. oras
stores arbitrary files as OCI artifacts without a Dockerfile.

## The file is pushed by basename from its own directory

oras validates the path in a `file:media:type` reference and rejects an absolute one. The flag
it offers is `--disable-path-validation`, which turns off **all** of its path checks - absolute
paths, `..`, symlinks, anything outside the working directory. Those checks exist so a mistyped
variable cannot push `~/.ssh/id_rsa` to a public package.

A bare filename, with the push run from the file's own directory, satisfies the check instead of
disabling it.

## Tags

| input | result |
|---|---|
| `kind: macos15-desktop` | `macos15-desktop-<UTC stamp>` |
| `kind: macos15-desktop`, `stamp: false` | `macos15-desktop` |
| `tag: my-exact-tag` | `my-exact-tag` |

A timestamp by default, because "which build is this" has to stay answerable and a disk is not
something to redeploy by accident.

## Outputs

`tag` and `reference`, written to `GITHUB_OUTPUT` so the next job can pull what this pushed.

## The token goes through stdin

`oras login -p <token>` puts the credential in the process table, where any other process on a
shared runner can read it. `--password-stdin` is the form that does not.
