"""python3 tests/test_download_guard.py - the halogen download: refuses before it asks when the disk is too small, a custom
folder is found by the launcher, and no script ever clones a model repo (~190 GB) instead of fetching named files."""
import glob, os, re, stat, subprocess, tempfile

REPO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
T = tempfile.mkdtemp()
home = os.path.join(T, "home")
os.makedirs(os.path.join(T, "bin"))
os.makedirs(home)


def stub(name, body):
    p = os.path.join(T, "bin", name)
    open(p, "w").write("#!/bin/sh\n" + body)
    os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR)


stub("df", 'echo "Avail"; echo "  5G"\n')  # every disk has 5 GB free
stub("hf", 'echo HF-WAS-CALLED >> "$HOME/hf-calls"; exit 1\n')  # must never run when the space check fails
env = dict(os.environ, HOME=home, PATH=f"{T}/bin:{os.environ['PATH']}", PI_CODING_AGENT_DIR=os.path.join(home, "agent"))
for k in ("HALOGEN_MODELS", "PI_HOME", "HALOGEN_WEIGHTS_REV"):
    env.pop(k, None)

# 1. not enough space: exits 1 BEFORE the "download now?" question (stdin is closed: a question would read EOF and abort differently),
#    says how much and where to put it instead, and never reaches hf
r = subprocess.run(["bash", f"{REPO}/scripts/download-halogen.sh"], env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
assert r.returncode == 1, r
assert "only 5 GB free" in r.stderr and "HALOGEN_MODELS=" in r.stderr and "download now?" not in r.stdout + r.stderr, (r.stdout, r.stderr)
assert not os.path.exists(f"{home}/hf-calls")

# 2. a folder that already holds part of the download counts toward what is needed (a resume)
part = os.path.join(home, "models", "halogen-qwen3.8-flash-next")
os.makedirs(part)
stub("du", 'echo "130G\t$2"\n')  # 130 GB already there: only ~5 GB missing, and 5 GB is free
r = subprocess.run(["bash", f"{REPO}/scripts/download-halogen.sh", "--yes"], env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
assert "only 5 GB free" not in r.stderr, r.stderr  # it got past the space check (and then reached the stub hf, which fails)
assert os.path.exists(f"{home}/hf-calls")

# 3. the launcher looks in HALOGEN_MODELS, and in the folder the download remembered
big = os.path.join(T, "bigdisk", "halogen-qwen3.8-flash-next")
os.makedirs(big)
open(os.path.join(big, "qwen38-flash-next-v2.hgn"), "w").write("x")
for e, label in (({"HALOGEN_MODELS": os.path.join(T, "bigdisk")}, "env"), ({}, "remembered")):
    en = dict(env, **e)
    if label == "remembered":
        os.makedirs(os.path.join(home, "agent"), exist_ok=True)
        open(os.path.join(home, "agent", "harness-models-dir"), "w").write(os.path.join(T, "bigdisk") + "\n")
    out = subprocess.run(["bash", f"{REPO}/server/start-halogen.sh", "--print-stock"], env=en, capture_output=True, text=True).stdout.strip()
    assert out == os.path.join(big, "qwen38-flash-next-v2.hgn"), (label, out)

# 4. no script clones or snapshot-downloads a whole model repo: every fetch names its files (hf_dl REPO files... / --include)
for f in glob.glob(f"{REPO}/scripts/*.sh") + glob.glob(f"{REPO}/server/*.sh") + [f"{REPO}/setup.sh"]:
    for n, line in enumerate(open(f), 1):
        s = line.split("#")[0]
        assert not re.search(r"git\s+clone\s+\S*huggingface\.co", s), f"{f}:{n} clones a model repo: {line.strip()}"
        m = re.search(r"\bhf_dl\s+(\S+)(.*)", s)
        if m and "--local-dir" in s and not re.search(r"\.(hgn|gguf)|--include|/\"|\"\$", m.group(2)) and "npu" not in m.group(1) and "$" not in m.group(1):
            raise AssertionError(f"{f}:{n} downloads a whole repo (name the files): {line.strip()}")
print("ok")
