#!/usr/bin/env python3
"""Stream a command's output into a check run, so an outside reader can follow it live.

USAGE (in a workflow step):

    - name: build
      run: python3 log_stream.py -- make test

WHY A CHECK RUN: a running job's Actions log is not readable through any public API — GitHub serves
it from an internal store, and the web endpoints want a browser session. A check run's
`output.text` IS readable while the job runs, with a plain GET and a PAT. This script keeps that
field up to date as the command produces output.

THE THREE THINGS THAT MAKE IT WORK, all learned by measurement:

  1. PATCH replaces `output` WHOLESALE. Sending {"output": {...}} without `text` ERASES the text,
     so every PATCH carries the whole buffer. There is no append.
  2. `output.text` is capped at 65535 characters. A chatty build blows through that in seconds, so
     the buffer keeps the TAIL — the newest output is the part a reader is waiting for — and says
     on the first line how many lines were dropped.
  3. The final PATCH sets status/conclusion AND carries the text in the same request. Splitting
     them is what silently emptied the field in my first attempt.

WHY IT THROTTLES: one PATCH per line would be tens of thousands of requests for a normal build.
Updates go out at most every --interval seconds, and only when the buffer actually changed.
"""
import argparse
import datetime
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

MAX_TEXT = 65535


class CheckRun:
    """The check run this step owns, and the text it keeps in it."""

    def __init__(self, token, repo, sha, name, interval, max_text):
        self.token = token
        self.repo = repo
        self.sha = sha
        self.name = name
        self.interval = interval
        self.max_text = max_text

        self.lines = []
        self.dropped = 0
        self.dirty = False
        self.lock = threading.Lock()
        self.id = None
        self._stop = threading.Event()
        self._last_sent = ""
        self.pushes = 0
        self.failures = 0

    # ---- HTTP ------------------------------------------------------------
    def _call(self, path, method="GET", body=None):
        url = "https://api.github.com/repos/" + self.repo + path
        req = urllib.request.Request(
            url, method=method,
            data=json.dumps(body).encode() if body else None,
            headers={"Authorization": "Bearer " + self.token,
                     "Accept": "application/vnd.github+json",
                     "Content-Type": "application/json"})
        try:
            r = urllib.request.urlopen(req, timeout=30)
            raw = r.read()
            return r.status, json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")[:300]
        except Exception as e:                        # network trouble: never fail the build for it
            return -1, str(e)[:200]

    # ---- text ------------------------------------------------------------
    def render(self):
        """The text to publish, trimmed to the tail if it would exceed the cap."""
        with self.lock:
            lines = list(self.lines)
            dropped = self.dropped
        while True:
            body = "\n".join(lines)
            if len(body) <= self.max_text:
                break
            # Drop from the front, in chunks, and remember how many lines went.
            cut = max(1, len(lines) // 10)
            dropped += cut
            lines = lines[cut:]
        if dropped:
            head = "[… %d earlier lines omitted — output.text holds %d chars …]" % (
                dropped, self.max_text)
            body = head + "\n" + body
            if len(body) > self.max_text:
                body = body[-self.max_text:]
        return body

    def feed(self, line):
        """Record one line of output, stamped exactly as the Actions log stamps it.

        FORMAT: 2026-10-26T13:35:32.7366402Z — the runner's own, to the digit.

        WHY MATCH IT: a compact HH:MM:SS would fit maybe a fifth more lines into the 65535-character
        budget. That is the whole argument for the short one, and it does not outweigh this one:
        the same line of build output carries the SAME timestamp in both places, so the live view
        and the archived log can be read against each other, grepped together, and sorted as one
        sequence. A format that looks slightly different in each place makes a reader stop and
        translate, which is the opposite of what a progress view is for.

        Seven fractional digits, because that is what the runner emits. Python's %f stops at
        microseconds, so the nanoseconds come from time.time_ns() and are formatted by hand —
        dropping to six digits would reintroduce exactly the mismatch this is meant to avoid.
        """
        ns = time.time_ns()
        secs, nanos = divmod(ns, 1_000_000_000)
        stamp = datetime.datetime.fromtimestamp(
            secs, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        stamp = "%s.%07dZ" % (stamp, nanos // 100)
        with self.lock:
            self.lines.append("%s %s" % (stamp, line.rstrip("\n")))
            self.dirty = True

    # ---- lifecycle -------------------------------------------------------
    def start(self):
        # external_id carries the workflow run id, and that is the whole point of setting it: a
        # check run has its own check_suite, unrelated to the workflow run's, so nothing in the
        # object otherwise says which run published it. With this field, a reader given a run id can
        # find exactly this text instead of guessing among every check run on the commit.
        st, res = self._call("/check-runs", "POST", {
            "name": self.name,
            "head_sha": self.sha,
            "external_id": os.environ.get("GITHUB_RUN_ID", ""),
            "status": "in_progress",
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "output": {"title": self.name, "summary": "streaming…", "text": "starting…"},
        })
        if st not in (200, 201):
            print("log-stream: cannot create a check run (%s): %s" % (st, res),
                  file=sys.stderr)
            return False
        self.id = res["id"]
        print("log-stream: check run %d, readable at %s" % (self.id, res.get("html_url", "")),
              file=sys.stderr)
        threading.Thread(target=self._loop, daemon=True).start()
        return True

    def _loop(self):
        while not self._stop.is_set():
            self._stop.wait(self.interval)
            if self._stop.is_set():
                break
            self._push()

    def _push(self):
        with self.lock:
            if not self.dirty:
                return
            self.dirty = False
        text = self.render()
        if text == self._last_sent:
            return
        st, res = self._call("/check-runs/%d" % self.id, "PATCH", {
            "status": "in_progress",
            "output": {"title": self.name,
                       "summary": "%d lines" % len(text.splitlines()),
                       "text": text},
        })
        if st == 200:
            self._last_sent = text
            self.pushes += 1
        else:
            self.failures += 1
            with self.lock:
                self.dirty = True          # try again next tick
            print("log-stream: push failed (%s): %s" % (st, res), file=sys.stderr)

    def finish(self, conclusion):
        self._stop.set()
        # One final push that carries BOTH the text and the conclusion: omitting output here is
        # what empties the field.
        text = self.render()
        st, res = self._call("/check-runs/%d" % self.id, "PATCH", {
            "status": "completed",
            "conclusion": conclusion,
            "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "output": {"title": self.name,
                       "summary": "%d lines" % len(text.splitlines()),
                       "text": text},
        })
        print("log-stream: finished %s (%d pushes, %d failures)"
              % (conclusion, self.pushes, self.failures), file=sys.stderr)
        return st == 200


def _group_of(pid: int) -> int:
    """The process group a pid belongs to, falling back to the pid itself.

    WHY THIS IS NOT `proc.pid`. `start_new_session=True` makes the CHILD a session and group
    leader, so `proc.pid == proc's pgid` - that part was right. What it missed is that the
    child is `bash -c "<the command>"`, and bash starts each simple command in a NEW PROCESS
    GROUP when it is not interactive. Measured on the reproduction:

        python(step)  pid=530069  pgid=530067
        bash -c       pid=530067  pgid=530067
        child         pid=530079  pgid=530079    <- its own group

    So killpg(proc.pid) signalled bash's group, bash died, and the grandchild - in a group of
    its own, trapping SIGTERM - kept running. The handler logged "signal 15", the step
    exited, and qemu's stand-in was still there; that is the whole of macos15 run
    42000179043, where a cancel was accepted twice and the step ran nine more minutes.

    Reading the real group with getpgid is the difference between signalling the pid we
    happen to hold and signalling the group the work is actually in. The fallback covers the
    race where the child has exited between the call and the lookup.
    """
    try:
        return os.getpgid(pid)
    except (ProcessLookupError, PermissionError, OSError):
        return pid


def _descendants(root: int) -> list[int]:
    """Every pid below `root`, via /proc. Empty when the tree is already gone.

    WHY THIS EXISTS ON TOP OF killpg. `start_new_session=True` makes the DIRECT child a
    session and group leader, so killing its group was assumed to reach everything. Measured
    on a reproduction built to match the runner's own process tree:

        python(step)  pid=530209  pgid=530207     <- not the leader: an outer bash holds that
        child         pid=530219  pgid=530219     <- its own group

    A killpg on the number `Popen` returned signalled a group that the work had already
    stepped out of. That is macos15 run 42000179043: the handler logged "signal 15", the
    step exited, and the guest was still booting nine minutes later.

    Reading the process table is the only answer that does not depend on which of bash,
    dash or the kernel created a group. Every descendant is found by ppid, from the root
    down, and each group they belong to is signalled as well.
    """
    children: dict[int, list[int]] = {}
    try:
        entries = os.listdir("/proc")
    except OSError:
        return []
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat", encoding="utf-8") as fh:
                # comm can contain spaces and brackets; the fields after the LAST ')' are
                # state, ppid, pgrp, ...
                fields = fh.read().rsplit(")", 1)[-1].split()
            ppid = int(fields[1])
        except (OSError, IndexError, ValueError):
            continue
        children.setdefault(ppid, []).append(int(entry))

    found: list[int] = []
    stack = [root]
    while stack:
        pid = stack.pop()
        for child in children.get(pid, []):
            if child not in found:
                found.append(child)
                stack.append(child)
    return found


def _kill_tree_of(pid: int, *, grace: float = 2.0) -> None:
    """Stop a command and everything it started.

    TWO PASSES, BECAUSE ONE IS NOT ENOUGH. The first signals the process group that `pid`
    is actually in (read with getpgid, not assumed to be `pid`), which covers the ordinary
    case where the child stayed in its leader's group. The second walks the process table
    for ppid descendants, which covers the case measured above: a grandchild that bash put
    in a group of its own.

    SIGTERM FIRST SO A GUEST CAN FLUSH. qemu and ffmpeg handle it; a disk killed mid-write
    repairs itself on the next boot, and that is a worse artifact than a slower cancel.
    """
    targets = {pid}
    descendants = _descendants(pid)
    targets.update(descendants)

    groups: set[int] = set()
    for target in targets:
        try:
            groups.add(os.getpgid(target))
        except (ProcessLookupError, PermissionError, OSError):
            pass

    for group in groups:
        try:
            os.killpg(group, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    for target in targets:
        try:
            os.kill(target, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass

    deadline = time.time() + grace
    while time.time() < deadline:
        if not any(_exists(p) for p in targets):
            return
        time.sleep(0.05)

    # SIGKILL, TO EVERY GROUP AND EVERY PID THAT IS LEFT. Unconditional: the immediate
    # child dies on SIGTERM and its children may not, which is the case that matters.
    for _ in range(2):
        alive_now = [p for p in targets if _exists(p)]
        if not alive_now:
            return
        for target in alive_now:
            try:
                os.kill(target, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                os.killpg(os.getpgid(target), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        time.sleep(0.1)


def _exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def main():
    p = argparse.ArgumentParser(
        description="Run a command and stream its output into a GitHub check run.")
    p.add_argument("--name", default=None,
                   help="check run name (default: the job name)")
    p.add_argument("--interval", type=float, default=5.0,
                   help="seconds between pushes (default 5)")
    p.add_argument("--max-bytes", type=int, default=MAX_TEXT,
                   help="text cap (default and maximum 65535)")
    p.add_argument("--continue-on-failure", action="store_true",
                   help="stream a failing command without failing the step")
    p.add_argument("command", nargs=argparse.REMAINDER,
                   help="-- followed by the command to run")
    args = p.parse_args()

    cmd = args.command
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        p.error("no command given; use: log_stream.py -- make test")

    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    sha = os.environ.get("GITHUB_SHA")
    if not (token and repo and sha):
        print("log-stream: GITHUB_TOKEN, GITHUB_REPOSITORY and GITHUB_SHA must be set "
              "(they are, in a workflow)", file=sys.stderr)
        return 2

    name = args.name or os.environ.get("GITHUB_JOB") or "build"
    cr = CheckRun(token, repo, sha, name, args.interval, min(args.max_bytes, MAX_TEXT))
    if not cr.start():
        # Streaming is a convenience, not the point of the build. Run the command anyway.
        #
        # BUT NOT WITH subprocess.call. That was here, and it is why a cancel was ignored on
        # macos15 run 42000179043: when the check run could not be created the step fell
        # through to `subprocess.call(cmd)`, which installs no signal handler, does not put
        # the child in its own session, and forwards nothing. The runner's SIGTERM killed
        # this process and left `bash -c "<the command>"` and qemu running - exactly the
        # failure the signal code below exists to prevent, on the one path that skipped it.
        #
        # THE DEGRADED PATH HAS TO BE AS CANCELLABLE AS THE STREAMING ONE. A build whose log
        # cannot be published is still a build somebody will want to stop.
        print("log-stream: continuing WITHOUT streaming", file=sys.stderr)
        plain = subprocess.Popen(cmd, start_new_session=True)

        def _kill_plain(signum, _frame):
            print(f"log-stream: signal {signum}, stopping the command's process group",
                  file=sys.stderr, flush=True)
            _kill_tree_of(plain.pid)
            os._exit(143)

        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(sig, _kill_plain)

        return plain.wait()

    # Forward the child's output to this step's log as well, so the Actions log is unchanged.
    #
    # text=True decodes as UTF-8 and RAISES on a byte sequence that is not valid
    # UTF-8, and that exception killed the step it was supposed to be streaming.
    # Measured, not theorised: ubuntu's bake died at two minutes with
    #
    #   File ".../log_stream.py", line 244, in main
    #     for line in proc.stdout:
    #   UnicodeDecodeError: utf-8 codec can't decode byte 0xe2 in position 1408
    #   ##[error]Process completed with exit code 1.
    #
    # The bake itself was fine - the guest was still drawing its kernel messages. A
    # serial console writes bytes, not text: escape sequences, and any line the
    # terminal cut in half mid-character, arrive however they arrive.
    #
    # errors='replace' turns the offending byte into a replacement character and keeps
    # going. Losing one glyph in a progress log is nothing next to losing the run.
    # ITS OWN PROCESS GROUP, SO A CANCEL REACHES WHAT THE COMMAND STARTED.
    #
    # The command is `bash -c "<the step's script>"`, and that script starts the real work -
    # python3, qemu, ffmpeg. Killing only the direct child leaves the grandchildren running:
    # bash dies, the Python it forked does not, and the step's log stops while the machine
    # keeps booting. That is what a cancelled macOS job did - the cancel was accepted twice,
    # the run stayed in_progress, and the guest went on waiting out a 1800-second timeout.
    #
    # start_new_session=True puts the child in a new session and process group, which makes
    # os.killpg able to reach every descendant in one call. Without it there is no handle on
    # anything below the first level: the child's children are in the SAME group as this
    # process, and killing that group would kill the streamer too.
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1, errors='replace',
                            start_new_session=True)

    # SIGNALS ARRIVING DURING THE RUN, AND SIGNALS ARRIVING AT ALL.
    #
    # THREE SEPARATE THINGS WERE WRONG, and each one alone was enough to make a cancel
    # look like it did nothing:
    #
    #   SIGTERM reached this process and called proc.terminate() - one PID. See above.
    #
    #   SIGINT is what a cancel sometimes arrives as, and nothing handled it, so Python
    #   turned it into KeyboardInterrupt inside the `for line in proc.stdout` loop - which
    #   leaves the loop (and the child) to the finally-less exit path.
    #
    #   SIGHUP comes when the runner tears the step down, and the default is to die without
    #   touching the child at all - the grandchild is orphaned onto PID 1 and runs to its
    #   own timeout.
    #
    # All three now do the same thing: kill the child's whole process group, then this
    # process. SIGKILL follows SIGTERM because a qemu that is mid-write ignores polite
    # requests, and a cancelled step must not wait for one.
    def _kill_tree(signum, frame):
        print(f"log-stream: signal {signum}, stopping the command's process group",
              file=sys.stderr, flush=True)
        _kill_tree_of(proc.pid)
        # THE STREAMER EXITS, WHICH IS WHAT LETS THE STEP FINISH. Exiting through the
        # normal path instead would run cr.finish("failure") and report a cancel as a
        # build failure.
        os._exit(143)

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, _kill_tree)

    for line in proc.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        cr.feed(line)
    code = proc.wait()

    conclusion = "success" if code == 0 else "failure"
    cr.finish(conclusion)
    if code != 0 and not args.continue_on_failure:
        return code
    return 0


if __name__ == "__main__":
    sys.exit(main())
