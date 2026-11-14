#!/usr/bin/env python3
"""Integration tests for tmux-program-status (stdlib Python 3 only).

Each test gets an isolated tmux server (project-named socket in a private
tempdir) and loads program-status.tmux as a Bash program with a real
TMUX=socket,pid,session environment. OSC 7501 / OSC 9;4 reports are written to
the pane tty, and `#{E:@program-status}` is read back through `display -p`,
which expands the format correctly even without an attached client.

Capabilities are found by emitting a real report and polling the field it is
supposed to populate: an empty field before any report proves nothing. Builds
with neither field (e.g. 3.4) skip protocol tests but still must render blank.

Usage: python3 tests/integration.py TMUX_BINARY
"""
import atexit
import base64
import os
import pty
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.join(os.path.dirname(HERE), "program-status.tmux")
ST = "\x1b\\"
POLL = 1.0
BIN = None
SERVERS = []


def unquote(value):
    """Collapse q/h doubled hashes back to the text the application sent."""
    return value.replace("##", "#")


def tmux_bin():
    explicit = next((a for a in sys.argv[1:] if not a.startswith("-")), None)
    return shutil.which(explicit or os.environ.get("TMUX_BIN") or "tmux")


class Server:
    def __init__(self):
        self.env = {k: v for k, v in os.environ.items() if k != "TMUX"}
        # Unix socket paths are length-limited; keep the socket in a short,
        # self-owned dir (never a shared prefix) so long TMPDIRs still work.
        self.sockdir = tempfile.mkdtemp(prefix="tps-sock-")
        self.sock = os.path.join(self.sockdir, "s")
        self.proc = None
        self.log = None
        self.client_fd = self.client_pid = None

    def run(self, *args, check=True):
        # tmux -N never starts a server: a client must not silently replace the
        # owned foreground child with an unowned daemon (tmux.1: -N).
        proc = subprocess.run([BIN, "-N", "-S", self.sock, *args], capture_output=True,
                              text=True, env=self.env, timeout=30)
        if check and proc.returncode != 0:
            raise AssertionError("tmux %s: %s" % (" ".join(args), proc.stderr.strip()))
        return proc

    def display(self, fmt, target="%0"):
        return self.run("display", "-p", "-t", target, fmt).stdout.rstrip("\n")

    def badge(self, target="%0"):
        return self.display("#{E:@program-status}", target)

    def start(self):
        SERVERS.append(self)
        # Foreground (-D) server: a direct, owned child instead of a daemon,
        # so teardown can wait on it (tmux.1 -D: no daemon, exit-empty off).
        self.log = tempfile.TemporaryFile()
        self.proc = subprocess.Popen(
            [BIN, "-D", "-S", self.sock, "-f", "/dev/null"],
            stdin=subprocess.DEVNULL, stdout=self.log, stderr=self.log,
            env=self.env)
        deadline = time.time() + 5.0
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise AssertionError("tmux server exited during startup")
            if os.path.exists(self.sock) and self.run(
                    "list-sessions", check=False).returncode == 0:
                break
            time.sleep(0.05)
        else:
            raise AssertionError("tmux server did not become ready")
        self.run("new-session", "-d", "-x", "220", "-y", "40",
                 "sleep 300")
        self.run("set", "-g", "status-interval", "0")

    def load(self, plugin=PLUGIN):
        session, pid = self.display("#{session_id} #{pid}").split()
        env = dict(self.env, TMUX="%s,%s,%s" % (self.sock, pid, session),
                   TMUX_PROGRAM_STATUS_TMUX=BIN)
        proc = subprocess.run(["bash", plugin], capture_output=True, text=True,
                              env=env, timeout=30)
        if proc.returncode != 0:
            raise AssertionError("plugin load failed: %s" % proc.stderr.strip())
        self.poll(lambda: self.run("show-options", "-gv", "@program-status",
                                   check=False).stdout.strip() != "", "plugin set option")

    def tty(self, target="%0"):
        return self.display("#{pane_tty}", target)

    def emit(self, data, target="%0"):
        fd = os.open(self.tty(target), os.O_WRONLY | os.O_NOCTTY)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)

    def native(self, state, app=None, msg=None):
        payload = "7501;state=" + state
        if app is not None:
            payload += ":app=" + app
        if msg is not None:
            payload += ":msg=" + base64.b64encode(msg.encode()).decode()
        self.emit(("\x1b]" + payload + ST).encode())

    def legacy(self, state, progress=None):
        payload = "9;4;%d" % state
        if progress is not None:
            payload += ";%d" % progress
        self.emit(("\x1b]" + payload + ST).encode())

    def poll(self, check, what, timeout=POLL):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if check():
                return
            time.sleep(0.05)
        raise AssertionError("%s (badge %r)" % (what, self.badge()))

    def assertBadge(self, expected, target="%0"):
        self.poll(lambda: unquote(self.badge(target)) == expected, "expected %r" % (expected,))

    def capability(self):
        """Set of protocols a real report populates; a bare field read proves nothing."""
        self.legacy(0)
        self.native("clear")
        time.sleep(0.2)
        found = set()
        self.native("working", app="probe")
        if self._settles("pane_program_state", "working"):
            found.add("native")
        self.native("clear")
        self.legacy(1, 37)
        if self._settles("pane_pb_state", "normal"):
            found.add("legacy")
        return found

    def _settles(self, field, want):
        deadline = time.time() + POLL
        while time.time() < deadline:
            if self.display("#{%s}" % field) == want:
                return True
            time.sleep(0.05)
        return False

    def attach(self):
        """Attach a real client under a pty; return bytes written to its terminal."""
        pid, fd = pty.fork()
        if pid == 0:
            child_env = dict(self.env, TERM="xterm-256color")
            try:
                os.execve(BIN, [BIN, "-S", self.sock, "attach"], child_env)
            except OSError as exc:
                # Hard-exit so inherited unittest cleanup never runs, even if
                # the pty write itself fails.
                try:
                    os.write(2, ("attach exec failed: %s\n" % exc).encode())
                finally:
                    os._exit(127)
        self.client_pid, self.client_fd = pid, fd
        out = self.drain(0.8)
        self.poll(lambda: self.run("list-clients", check=False).stdout.strip() != "",
                  "client registered")
        self.run("refresh-client", "-S", check=False)
        return out + self.drain(0.6)

    def drain(self, seconds):
        out = b""
        deadline = time.time() + seconds
        while time.time() < deadline:
            ready, _, _ = select.select([self.client_fd], [], [], 0.05)
            if ready:
                try:
                    chunk = os.read(self.client_fd, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                out += chunk
        return out

    def kill(self):
        if self.client_fd is not None:
            try:
                os.close(self.client_fd)
            except OSError:
                pass
            self.client_fd = None
        if self.client_pid is not None:
            try:
                os.kill(self.client_pid, signal.SIGTERM)
                os.waitpid(self.client_pid, 0)
            except (ProcessLookupError, ChildProcessError):
                pass
            self.client_pid = None
        self.run("kill-server", check=False)
        if self.proc is not None:
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                self.proc.wait(timeout=5)
            self.proc = None
        if self.log is not None:
            self.log.close()
            self.log = None
        shutil.rmtree(self.sockdir, ignore_errors=True)


def cleanup():
    while SERVERS:
        SERVERS.pop().kill()


atexit.register(cleanup)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="tmux-program-status-test-")
        self.tm = Server()
        # Register cleanup before anything that can fail or skip, so a probe
        # failure still tears down the server and tempdir.
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        self.addCleanup(self._kill)
        self.tm.start()
        self.tm.load()
        self.kind = self.tm.capability()
        # Leave the pane blank; the probe report must not bleed into the test.
        self.tm.native("clear")
        self.tm.legacy(0)
        self.tm.poll(lambda: self.tm.badge() == "", "blank badge after probe")

    def _kill(self):
        SERVERS[:] = [s for s in SERVERS if s is not self.tm]
        self.tm.kill()

    def need(self, *kinds):
        missing = [k for k in kinds if k not in self.kind]
        if missing:
            if os.environ.get("TMUX_PROGRAM_STATUS_REQUIRE_FEATURES") == "1":
                self.fail("required capability %s missing (probe found %s)"
                          % (", ".join(missing), sorted(self.kind) or "none"))
            self.skipTest("probe found %s, need %s"
                          % (sorted(self.kind) or "none", ", ".join(missing)))


class NativeStates(Base):
    def setUp(self):
        super().setUp()
        self.need("native", "legacy")

    def test_each_state(self):
        for state in ("idle", "working", "done", "blocked", "error"):
            self.tm.native(state)
            self.tm.assertBadge("[%s]" % state)

    def test_app_and_message(self):
        self.tm.native("working", app="claude", msg="Building project")
        self.tm.assertBadge("[working claude: Building project]")
        self.tm.native("done", app="go")
        self.tm.assertBadge("[done go]")
        self.tm.native("idle", msg="Waiting")
        self.tm.assertBadge("[idle: Waiting]")

    def test_native_wins_and_no_inherited_percent(self):
        self.tm.legacy(1, 10)
        self.tm.assertBadge("[working 10%]")
        self.tm.native("idle")
        self.tm.assertBadge("[idle]")
        self.tm.legacy(2, 90)
        self.tm.assertBadge("[idle]")

    def test_clear_falls_back_to_stored_legacy(self):
        self.tm.legacy(1, 40)
        self.tm.assertBadge("[working 40%]")
        self.tm.native("done", msg="Finished")
        self.tm.assertBadge("[done: Finished]")
        self.tm.native("clear")
        self.tm.assertBadge("[working 40%]")
        self.tm.legacy(0)
        self.tm.assertBadge("")

    def test_report_without_state_is_discarded(self):
        self.tm.emit(("\x1b]7501;app=lost:msg=aGk=" + ST).encode())
        self.tm.assertBadge("")

    def test_legacy_protocol_still_works_on_native_build(self):
        # Both protocols are exercised on the native build: with no native
        # state set, every legacy state must render its badge.
        self.tm.legacy(1, 37)
        self.tm.assertBadge("[working 37%]")
        self.tm.legacy(3)
        self.tm.assertBadge("[working]")
        self.tm.legacy(4, 20)
        self.tm.assertBadge("[paused 20%]")
        self.tm.legacy(2, 7)
        self.tm.assertBadge("[error 7%]")
        self.tm.legacy(1, 100)
        self.tm.assertBadge("[working 100%]")
        self.tm.legacy(0)
        self.tm.assertBadge("")


class LegacyStates(Base):
    def setUp(self):
        super().setUp()
        self.need("legacy")

    def test_working_states(self):
        self.tm.legacy(1, 55)
        self.tm.assertBadge("[working 55%]")
        self.tm.legacy(1, 100)
        self.tm.assertBadge("[working 100%]")
        self.tm.legacy(3)
        self.tm.assertBadge("[working]")

    def test_labels_do_not_infer_success_or_blocked(self):
        self.tm.legacy(4, 20)
        self.tm.assertBadge("[paused 20%]")
        self.tm.legacy(2, 7)
        self.tm.assertBadge("[error 7%]")

    def test_indeterminate_has_no_percent(self):
        self.tm.legacy(1, 30)
        self.tm.assertBadge("[working 30%]")
        self.tm.legacy(3)
        self.tm.assertBadge("[working]")

    def test_hidden_is_empty(self):
        self.tm.legacy(1, 30)
        self.tm.assertBadge("[working 30%]")
        self.tm.legacy(0)
        self.tm.assertBadge("")


class FieldMissing(Base):
    def test_blank_badge_and_empty_fields(self):
        # This class only applies to builds with neither field; on a capable
        # build it is correctly skipped even under REQUIRE_FEATURES.
        if self.kind:
            self.skipTest("build supports %s" % sorted(self.kind))
        self.assertEqual(self.tm.badge(), "")
        self.assertEqual(self.tm.display("#{pane_program_state}"), "")
        self.assertEqual(self.tm.display("#{pane_pb_state}"), "")


class PaneContext(Base):
    def test_panes_are_independent(self):
        self.need("native", "legacy")
        self.tm.run("split-window", "-d", "-t", "0", "sleep 300")
        panes = self.tm.run("list-panes", "-t", "0", "-F", "#{pane_id}").stdout.split()
        self.assertEqual(len(panes), 2)
        self.tm.native("working", app="one")
        self.tm.legacy(2, 5)
        # second pane gets only legacy
        self.tm.emit(b"\x1b]9;4;2;5" + ST.encode(), panes[1])
        self.tm.assertBadge("[working one]", panes[0])
        self.tm.assertBadge("[error 5%]", panes[1])


class HostileData(Base):
    def test_data_is_literal_and_cannot_execute(self):
        self.need("native")
        marker = os.path.join(self.tmpdir, "PWNED")
        hostile = "#[fg=red] #{pane_id} #(touch %s) #{}" % marker
        self.tm.native("error", app="safe", msg=hostile)
        # The option value itself keeps doubled hashes (q/h quoting).
        self.assertIn("##{pane_id}", self.tm.badge())
        self.tm.poll(lambda: "#[fg=red]" in unquote(self.tm.badge()), "literal style text")
        # tmux rewrites "#(" to "_(" before storing application text.
        self.assertIn("_(touch", unquote(self.tm.badge()))
        self.assertNotRegex(unquote(self.tm.badge()), r"\x1b\[")
        self.assertFalse(os.path.exists(marker), "injected #() executed")

    def test_status_line_renders_literal_text(self):
        self.need("native")
        self.tm.run("set", "-g", "status-left-length", "160")
        self.tm.run("set", "-g", "status-left", "HEAD>#{E:@program-status}<END")
        self.tm.native("working", app="claude", msg="Doing #{x} #[fg=red] bold")
        out = self.tm.attach() + self.tm.drain(0.6)
        self.assertIn(b"HEAD>", out)
        self.assertIn(b"#[fg=red]", out)
        self.assertIn(b"#{x}", out)
        self.assertNotIn(b"\x1b[31m", out, "injected style produced red SGR")

    def test_border_and_window_status_render_badge(self):
        self.need("native")
        self.tm.run("split-window", "-d", "-t", "0", "sleep 300")
        # A second window makes one non-current so window-status-format renders.
        self.tm.run("new-window", "-d", "-t", "1", "sleep 300")
        self.tm.run("set", "-g", "pane-border-status", "top")
        self.tm.run("set", "-g", "pane-border-format", "BOR>#{E:@program-status}<")
        self.tm.run("set", "-g", "window-status-format", "WS>#{E:@program-status}<")
        self.tm.run("set", "-g", "window-status-current-format",
                    "WC>#{E:@program-status}<")
        # native()/badge() default to %0, so target each window's own pane.
        for window in ("1", "0"):
            pane = self.tm.display("#{pane_id}", window + ".0")
            self.tm.emit(b"\x1b]7501;state=working:app=solo\x1b\\", pane)
            self.tm.poll(lambda: unquote(self.tm.badge(pane)) == "[working solo]",
                         "badge on %s" % pane)
        self.tm.run("select-window", "-t", "0")
        out = self.tm.attach() + self.tm.drain(0.6)
        self.assertIn(b"BOR>[working solo]<", out, "pane border context")
        self.assertIn(b"WS>[working solo]<", out, "window-status-format context")
        self.assertIn(b"WC>[working solo]<", out, "window-status-current-format context")


class Runtime(Base):
    def test_only_own_option_and_repeat_load(self):
        def options():
            return dict(line.split(" ", 1) for line in self.tm.run(
                "show-options", "-g").stdout.splitlines() if " " in line)

        self.tm.run("set", "-gu", "@program-status", check=False)
        before = options()
        self.tm.load()
        first = self.tm.run("show-options", "-gv", "@program-status").stdout
        self.tm.load()
        self.tm.load()
        self.assertEqual(self.tm.run("show-options", "-gv", "@program-status").stdout, first)
        after = options()
        changed = sorted(k for k in set(before) | set(after)
                         if before.get(k) != after.get(k))
        self.assertEqual(changed, ["@program-status"])

    def test_existing_config_keys_hooks_pipe_untouched(self):
        conf = os.path.join(self.tmpdir, "user.conf")
        with open(conf, "w") as handle:
            handle.write("set -g status-left 'LEFT'\nset -g @custom keepme\n"
                         "bind-key X kill-pane\n"
                         "set-hook -g session-created 'display-message hooked'\n")
        self.tm.run("source-file", conf)
        keys = self.tm.run("list-keys").stdout
        hooks = self.tm.run("show-hooks", "-g").stdout
        self.tm.load()
        self.assertEqual(self.tm.run("list-keys").stdout, keys)
        self.assertEqual(self.tm.run("show-hooks", "-g").stdout, hooks)
        self.assertEqual(self.tm.display("#{@custom}"), "keepme")
        self.assertIn("LEFT", self.tm.display("#{status-left}"))
        self.assertEqual(self.tm.display("#{pane_pipe}"), "0")

    def test_source_path_with_spaces(self):
        spaced = os.path.join(self.tmpdir, "dir with spaces")
        os.mkdir(spaced)
        dest = os.path.join(spaced, "program-status.tmux")
        shutil.copy(PLUGIN, dest)
        self.tm.run("set", "-gu", "@program-status", check=False)
        self.tm.load(dest)

    def test_bootstrap_error_propagates(self):
        proc = subprocess.run(["bash", PLUGIN], capture_output=True, text=True,
                              env=dict(os.environ, TMUX_PROGRAM_STATUS_TMUX="/nonexistent/tmux"))
        self.assertNotEqual(proc.returncode, 0)


def main():
    global BIN
    if not os.access(PLUGIN, os.X_OK):
        sys.exit("program-status.tmux is not executable")
    BIN = tmux_bin()
    if not BIN:
        sys.exit("tmux binary not found")
    print("tmux binary:", BIN)
    print("tmux version:", subprocess.run([BIN, "-V"], capture_output=True,
                                          text=True).stdout.strip())
    result = unittest.main(argv=[sys.argv[0]], verbosity=2, exit=False)
    sys.exit(0 if result.result.wasSuccessful() else 1)


def _terminate(signum, _frame):
    cleanup()
    sys.exit(128 + signum)


for _sig in (signal.SIGINT, signal.SIGTERM):
    signal.signal(_sig, _terminate)

if __name__ == "__main__":
    main()
