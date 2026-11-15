"""python3 tests/test_prompts.py - the wizard's and the shell's questions: a typo in a long menu asks again (it used to pick
option 1), Esc closes the settings menu, a long question wraps inside a 40-column terminal, Ctrl-C/Ctrl-D/Esc cancel the
shell prompt, and with no terminal the default is taken and said."""
import builtins, contextlib, fcntl, io, os, pty, re, select, struct, subprocess, sys, termios, time

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "setup"))
import harness as h

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


class Tty:
    def isatty(self):
        return True


class NoTty:
    def isatty(self):
        return False


@contextlib.contextmanager
def fake(stdin, keys=None, inputs=None):
    """Swap stdin, key(), input() and gum lookup for the test; restored afterwards."""
    saved = sys.stdin, h.key, builtins.input, h.shutil.which
    sys.stdin = stdin
    if keys is not None:
        it = iter(keys)
        h.key = lambda: next(it)
    if inputs is not None:
        it2 = iter(inputs)
        builtins.input = lambda prompt="": next(it2)
    h.shutil.which = lambda c, *a, **k: None  # no gum: the plain prompts
    try:
        yield
    finally:
        sys.stdin, h.key, builtins.input, h.shutil.which = saved


def run_menu(options, esc=None, keys=None, inputs=None):
    buf = io.StringIO()
    with fake(Tty(), keys=keys, inputs=inputs), contextlib.redirect_stdout(buf):
        try:
            return h.choose("Which?", options, False, esc=esc), buf.getvalue()
        except SystemExit as e:
            return ("exit", e.code), buf.getvalue()


ten = [f"option {i}" for i in range(1, 11)]

# a typo in a menu of 10 asks again; a real number picks; Enter picks the first
r, out = run_menu(ten, inputs=["abc", "99", "3"])
assert r == "option 3" and "type 1-10" in out, (r, out)
r, _ = run_menu(ten, inputs=[""])
assert r == "option 1", r
# Esc in the long menu cancels, or returns the menu's own answer (settings: done)
r, _ = run_menu(ten, inputs=["\x1b"])
assert r == ("exit", 130), r
r, _ = run_menu(ten, esc="done", inputs=["\x1b"])
assert r == "done", r
print("ok: long menus re-ask on a typo; Esc cancels, or closes the settings menu")

# short menus: a bad key says what works and asks again; Esc closes the settings menu
r, out = run_menu(["a", "b", "c"], keys=["x", "2"])
assert r == "b" and "type 1-3" in out, (r, out)
r, _ = run_menu(["a", "b", "c"], esc="done", keys=["\x1b"])
assert r == "done", r
r, _ = run_menu(["a", "b", "c"], keys=["\x1b"])
assert r == ("exit", 130), r
print("ok: short menus hint on a bad key; Esc closes settings")

# Esc in a submenu goes back one level (returns BACK) instead of quitting; the top level still closes quietly
r, out = run_menu(["a", "b"], esc=h.BACK, keys=["\x1b"])
assert r is h.BACK, r
assert "Esc = back" in out, out
r, out = run_menu(["a", "b"], esc="done", keys=["\x1b"])
assert r == "done" and "Esc = done" in out, (r, out)
# a submenu that is not in back mode still cancels (the wizard is unchanged)
r, _ = run_menu(["a", "b"], keys=["\x1b"])
assert r == ("exit", 130), r
print("ok: Esc = back in submenus, Esc = done at the top, cancels elsewhere")

# gum exits 1 for Esc and 130 for Ctrl-C: 1 goes back, 130 still quits everything
class FakeGum:
    def __init__(self, code):
        self.returncode, self.stdout = code, ""


for code, expect in ((1, h.BACK), (130, ("exit", 130))):
    buf = io.StringIO()
    real_run = h.subprocess.run
    try:
        with fake(Tty()), contextlib.redirect_stdout(buf):
            h.shutil.which = lambda c, *a, **k: "/usr/bin/gum"  # after fake(): it hides gum on entry
            h.subprocess.run = lambda *a, **k: FakeGum(code)
            try:
                got = h.choose("Which?", ["a", "b"], False, esc=h.BACK)
            except SystemExit as e:
                got = ("exit", e.code)
    finally:
        h.subprocess.run = real_run
    assert got == expect, (code, got)
print("ok: gum Esc (1) goes back, gum Ctrl-C (130) quits")

# the config submenus return BACK instead of exiting, and write nothing when they do
buf = io.StringIO()
with fake(Tty(), keys=["\x1b"]), contextlib.redirect_stdout(buf):
    assert h.ask_model("strata", back=True) is h.BACK, "ask_model did not go back"
assert "Esc = back" in buf.getvalue(), buf.getvalue()
buf = io.StringIO()
with fake(Tty(), keys=["\x1b"]), contextlib.redirect_stdout(buf):
    assert h.ask_weights("halogen", back=True) is h.BACK, "ask_weights did not go back"
print("ok: model and weights submenus go back")

# the question: headline first, detail under it
assert h.question_text("Run it? ~2x faster commits, holds GPU memory.") == "Run it?\n  ~2x faster commits, holds GPU memory.", h.question_text("x? y")
assert h.question_text("Continue?") == "Continue?"
# a trailing question mark is the headline's end, not the whole sentence (this used to be one long wrapped line)
assert h.head_detail("Download it? ~124 GB into /m, 900 GB free") == ("Download it?", "~124 GB into /m, 900 GB free")
assert h.head_detail("No question mark here") == ("No question mark here", "")
print("ok: question split into headline and detail")

# every question in the wizard keeps a one-line headline: gum cuts a header off at the window edge instead of wrapping it
import ast
_tree = ast.parse(open(os.path.join(ROOT, "setup", "harness.py")).read())
_wide = []
for _n in ast.walk(_tree):
    if isinstance(_n, ast.Call) and isinstance(_n.func, ast.Name) and _n.func.id in ("choose", "ask_yn", "ask_once"):
        _i = 0 if _n.func.id != "ask_once" else 1
        if len(_n.args) > _i:
            _a = _n.args[_i]
            if isinstance(_a, ast.Constant) and isinstance(_a.value, str) and "{" not in _a.value:
                _head = h.head_detail(_a.value)[0]
                if len(_head) > 62:
                    _wide.append((_n.lineno, _head))
assert not _wide, _wide
print("ok: every question headline fits one line")

# no terminal: the default is taken and the output says which answer it was
buf = io.StringIO()
with fake(NoTty()), contextlib.redirect_stdout(buf):
    assert h.ask_yn("Start it?", True) is True
    assert h.choose("Which?", ["a", "b"], False) == "a"
assert "Start it? -> yes (no terminal: default)" in buf.getvalue() and "Which? -> a (no terminal: default)" in buf.getvalue(), buf.getvalue()
print("ok: no terminal takes the default and says so")


def pty_run(argv, cols, keys=(), env_extra=None):
    """Run argv on a real pseudo-terminal of `cols` columns, send the keys one by one, return (exit code, plain output)."""
    m, s = pty.openpty()
    fcntl.ioctl(s, termios.TIOCSWINSZ, struct.pack("HHHH", 24, cols, 0, 0))

    def controlling():  # the pty becomes the controlling terminal, so Ctrl-C reaches the child
        os.setsid()
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    env = {k: v for k, v in os.environ.items() if k not in ("COLUMNS", "LINES")}  # COLUMNS would override the size
    env.update(env_extra or {})
    p = subprocess.Popen(argv, stdin=s, stdout=s, stderr=s, preexec_fn=controlling, env=env, close_fds=True)
    os.close(s)
    out, t0, sent = b"", time.time(), 0
    while time.time() - t0 < 8:
        if sent < len(keys) and time.time() - t0 > 0.6 + 0.4 * sent:
            os.write(m, keys[sent]); sent += 1
        if select.select([m], [], [], 0.1)[0]:
            try:
                out += os.read(m, 4096)
            except OSError:
                break
        elif p.poll() is not None:
            break
    rc = p.poll()
    if rc is None:  # printed already but not reaped yet: give the exit a moment, do not call that a hang
        try:
            rc = p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
            rc = "hung"
    return rc, ANSI.sub("", out.decode(errors="replace")).replace("\r", "")


# the real yes/no in a 40-column terminal: no line runs past the edge, and Enter takes the default (yes)
PY_YN = ("import sys; sys.path.insert(0, sys.argv[1]); import harness as h; h.shutil.which = lambda c, *a, **k: None\n"
         "print('ANSWER', h.ask_yn(sys.argv[2], True))")
long_q = "Run the ling-tiny sidecar? ~2x faster commits and compaction, holds about 8 GiB of GPU memory (No = the main model writes them)."
rc, out = pty_run([sys.executable, "-c", PY_YN, os.path.join(ROOT, "setup"), long_q], 40, keys=(b"\r",),
                  env_extra={"PI_CODING_AGENT_DIR": "/tmp/prompt-test-agent"})
assert rc == 0 and "ANSWER True" in out, (rc, out)
for line in out.splitlines():
    assert len(line) <= 40, (len(line), line)
assert "(Enter = yes, Esc cancels)" in out, out
print("ok: a long question wraps inside 40 columns")

# a maximized window must not give very long lines: the wrap stops at 78 columns even in a 200-column terminal
rc, out = pty_run([sys.executable, "-c", PY_YN, os.path.join(ROOT, "setup"), long_q], 200, keys=(b"\r",),
                  env_extra={"PI_CODING_AGENT_DIR": "/tmp/prompt-test-agent"})
assert rc == 0 and "ANSWER True" in out, (rc, out)
longest = max(len(line) for line in out.splitlines())
assert longest <= 78, (longest, out)
print("ok: a wide terminal still wraps at 78 columns")

# shell prompt: Enter takes the default, y answers; Esc+Enter, Ctrl-C and Ctrl-D cancel (exit 130); no terminal aborts
SH = ["bash", "-c", f"source {os.path.join(ROOT, 'scripts', 'lib-prompt.sh')}; prompt_yn 'download now?' n 'about 4 GB'; echo rc=$?"]
rc, out = pty_run(SH, 80, keys=(b"y\r",))
assert rc == 0 and "rc=0" in out and "about 4 GB" in out, (rc, out)
rc, out = pty_run(SH, 80, keys=(b"\r",))
assert rc == 0 and "rc=1" in out, (rc, out)
for keys in [(b"\x1b\r",), (b"\x03",), (b"\x04",)]:
    rc, out = pty_run(SH, 80, keys=keys)
    assert rc == 130 and "cancelled" in out and "rc=" not in out, (keys, rc, out)
r = subprocess.run(SH, stdin=subprocess.DEVNULL, capture_output=True, text=True)
assert r.returncode == 1 and "aborted" in r.stderr, (r.returncode, r.stderr)
print("ok: shell prompt: default, answers, cancel on Esc / Ctrl-C / Ctrl-D, aborts with no terminal")
