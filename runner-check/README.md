# runner-check

Asserts the two things a VM job needs before it starts, and says what to do when they are
missing.

```yaml
- name: prepare
  uses: easydev-org/easyactions/runner-check@main
  with:
    kvm: true
    disk-gb: 10
```

## Why an action

`python3 lib/ci.py kvm` and `free-disk --need N` appeared in twenty-six places across one
repository's workflows, and both of the things that go wrong with a copied block went wrong:

- a fix had to be made in every copy, and one was always missed
- the number was chosen per copy: `launch (cosmic)` asked for 40 GB to run a 3769 MiB disk
  and failed on a runner with 11 GB free, next to twelve jobs that passed on the same image

`disk-gb` is still per stage - the right number really does differ - but it is no longer per
copy.

## Inputs

| input | default | |
|---|---|---|
| `kvm` | `true` | set false for a job that only moves files |
| `disk-gb` | `0` | free GB needed; 0 skips the check |
| `prune` | `true` | delete the hosted toolchains when space is short |

## What each check does

**`kvm`** opens `/dev/kvm` read-write. Not `os.access`: the node is `0o660` owned by a
group, and a job uid that is in that group still gets "no write permission" from
`os.access`. A version of this check that used it failed every bake on a runner where KVM
worked perfectly. If the open fails and the process is root, it chmods the node; if it is
not, it tries `sudo chmod`, and if neither is possible it explains that the job needs
`options: --device /dev/kvm`.

**`disk-gb`** counts free space, and when there is not enough it frees what is safe and
counts again before failing:

```
docker system prune -af            images, containers, build cache
docker builder prune -af           the build cache alone
/usr/share/dotnet                  the hosted .NET SDK
/usr/local/lib/android             the hosted Android SDK
/opt/ghc                           the hosted GHC toolchain
/opt/hostedtoolcache/CodeQL        the CodeQL bundle
```

`/opt/hostedtoolcache` and `/usr/local/lib/node_modules` are **never** touched: Actions reads
the first to resolve tool versions and the runner's own JavaScript actions live in the
second, so deleting either makes a later step fail with an error that has nothing to do with
disk space.

Every delete has a timeout. `docker system prune` has been measured at 984 seconds doing
nothing, and a prune that hangs must not hold the job.

## Exit codes

| | |
|---|---|
| 0 | ready |
| 1 | a check failed; the message names the fix |
| 2 | `disk-gb` was not an integer |
