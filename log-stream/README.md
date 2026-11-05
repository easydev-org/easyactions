# log-stream

Runs a command and publishes its output into a GitHub check run while it runs.

```yaml
- name: bake
  uses: easydev-org/easyactions/log-stream@main
  with:
    token: ${{ github.token }}
    name: macos15-setup
    interval: "20"
    command: |
      set -euo pipefail
      python3 macos/15/bake.py --out out/macos15.qcow2
```

## Why

The Actions log of a running step is not readable until the step ends. A check run's
`output.text` is. A bake that takes ninety minutes and fails at minute twelve is worth
following at minute thirteen.

## Inputs

| input | default | |
|---|---|---|
| `command` | required | passed to `bash -c` |
| `token` | required | the caller's workflow token |
| `name` | the job name | the check run's name |
| `interval` | `5` | seconds between pushes |
| `max_bytes` | `65535` | text cap; GitHub refuses more |

## Cancellation

A cancelled step must stop the machine it started. The command is run in its own session
(`start_new_session=True`) so that the whole process tree is one process group, and SIGTERM,
SIGINT and SIGHUP all kill that group - five seconds of SIGTERM, then SIGKILL.

Without this, a cancel kills the `bash -c` wrapper, its children are re-parented to PID 1, the
guest keeps booting, and the job stays `in_progress` until the command's own timeout. That is
not a theory; it happened to a macOS job whose cancel was accepted twice and which carried on
for another half hour.

## Streaming is a convenience

If the check run cannot be created - no token, no network, a rate limit - the command still
runs, and the step still fails or succeeds on its exit code. The log is what you lose.
