# easyactions

Reusable GitHub Actions for this organisation. Each directory is one action, used as

```yaml
- uses: easydev-org/easyactions/log-stream@main
```

## What is here

| action | what it does |
|---|---|
| [`log-stream`](log-stream/) | Runs a command and publishes its output to a check run while it runs, so a build can be followed live instead of after it finishes. Also the one place a cancel is turned into a real process-tree kill. |

## Why a separate repository

An action that a workflow depends on is a different kind of thing from a script in the
workflow's own repository, and the difference shows up when the action is wrong:

- a bug here affects every workflow that uses it, so it is fixed once instead of eighteen times
- a change here does not need the eighteen workflows to be re-run to take effect
- the failure mode of a bad action is "the step hung", which is worth one repository with
  tests rather than eighteen YAML files that each embed a copy

The workflows in `easydev-org/easydiskbake` still call the older
`easyagent-github-ext/actions/log-stream@main`; they move here one at a time, and the version
here is the one with the process-group fix.

## The rule about cancellation

Every action here that runs a command must be cancellable, and "cancellable" means the whole
process tree stops - not that the step's log stops. `gh run cancel` is a signal to the runner,
the runner signals the step, and an action that kills only its direct child leaves the real
work running with its output cut off. `log-stream` is the worked example: `start_new_session=True`
and `os.killpg`, and SIGTERM, SIGINT and SIGHUP all handled.
