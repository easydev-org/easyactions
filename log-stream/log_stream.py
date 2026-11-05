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
        print("log-stream: continuing WITHOUT streaming", file=sys.stderr)
        return subprocess.call(cmd)

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
        # `proc.pid` IS THE GROUP ID. start_new_session=True makes the child a session and
        # group leader, so its pid and its pgid are the same number - asking the kernel for
        # getpgid() can return the group after a race where the child has already exited,
        # and the answer is known here without asking.
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        # GIVE IT FIVE SECONDS, THEN STOP ASKING.
        #
        # qemu and ffmpeg handle SIGTERM; a shell loop that ignores it does not. Five
        # seconds is long enough for the first and short enough that the second cannot
        # hold the step open - which is the whole complaint this function exists to fix.
        deadline = time.time() + 5.0
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            time.sleep(0.1)
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
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
