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

## No dependency beyond curl

THIS USED TO BE `log_stream.py`, AND THE IMAGE THAT NEEDS LIVE LOGS MOST IS THE ONE WITHOUT AN
INTERPRETER. `easydev-org/easyvm` removed `python3` on purpose - its driver is Go and speaks QMP
directly - so this action refused to start in exactly the place a three-hour leg lives.

`.github/log-stream.sh` does the same job with two HTTP requests and no `jq` either: the check-run id
is read out of a response the script generated itself. THE ENTIRE DEPENDENCY IS `curl`.

| was | now | why |
|---|---|---|
| `python3 log_stream.py` | `sh lib/log_stream.sh` | the caller's image has no interpreter |
| `json.dumps` | `sed` | no `jq`, and the payload is one known shape |
| `start_new_session` + `os.killpg` | `kill_tree` over `pgrep -P` | `setsid` forks, so `$!` is the wrapper |
| argv flags | `INPUT_*` environment | that is how a composite action passes inputs |

FOUR BUGS WERE FOUND BY RUNNING IT, AND NONE WOULD HAVE FAILED LOUDLY IN CI:

- the id regex required no space, and GitHub answers `{"id": 4242` - the check run was created, the
  script decided it had failed, and the command ran unpublished
- a raw newline in the JSON: a string may not contain a control character, and a log is mostly
  newlines, so every update was refused once per interval with nothing in the log to say so
- a raw tab, the same way
- `set -m` prints `can't access tty` in a non-interactive shell, and a shell option is not a property
  of the child

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
