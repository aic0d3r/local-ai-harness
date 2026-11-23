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

# 5. a model file that is there but short (a killed download, a bad disk) is not "already there": the fast path verifies
#    against the repo listing (a local HF_ENDPOINT here: sizes only) and exits 1 naming the file; whole files pass as before
import http.server, json, threading

first = subprocess.run(["bash", f"{REPO}/server/start-gufo-flashnext.sh", "--print-model"], env=dict(env, GUFO_WEIGHTS="stock"), capture_output=True, text=True).stdout.strip()
sdir, root, n = os.path.dirname(first), os.path.dirname(os.path.dirname(first)), int(re.search(r"-of-0*(\d+)\.gguf$", first).group(1))
shards = [first.replace("-00001-of-", f"-{i:05d}-of-") for i in range(1, n + 1)]
mtp, proj = "mtp-Qwen3.8-Flash-Next-shared-Q8_0.gguf", "mmproj-BF16.gguf"
os.makedirs(sdir)
for f in shards:
    open(f, "w").write("abc")  # 3 bytes each
open(os.path.join(root, mtp), "w").write("m")
open(os.path.join(root, proj), "w").write("p")
repo_prefix = os.path.relpath(sdir, root)
SIZES = {f"{repo_prefix}/{os.path.basename(f)}": 3 for f in shards}  # what the "repo" says: whole at first
SIZES[proj] = 1


class Listing(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        b = json.dumps([{"type": "file", "path": p, "size": s, "lfs": {"size": s, "oid": None}} for p, s in SIZES.items() if p.startswith(repo_prefix) or p == proj]).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)


ls = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Listing)
threading.Thread(target=ls.serve_forever, daemon=True).start()
env["HF_ENDPOINT"] = f"http://127.0.0.1:{ls.server_port}"
os.remove(f"{home}/hf-calls")
r = subprocess.run(["bash", f"{REPO}/scripts/download-stock.sh", "--yes"], env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
assert r.returncode == 0 and "already there" in r.stdout and "verified" in r.stdout, (r.returncode, r.stdout, r.stderr)
assert not os.path.exists(f"{home}/hf-calls"), "a complete, whole download must not call hf"
SIZES[f"{repo_prefix}/{os.path.basename(shards[1])}"] = 3000  # the repo's real size: the file on disk is cut short
r = subprocess.run(["bash", f"{REPO}/scripts/download-stock.sh", "--yes"], env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
assert r.returncode == 1 and "cut short" in r.stderr and os.path.basename(shards[1]) in r.stderr and "already there" not in r.stdout, (r.returncode, r.stdout, r.stderr)
assert not os.path.exists(f"{home}/hf-calls"), "a bad file is deleted by hand, never re-fetched over (hf skips present files)"
ls.shutdown()
print("ok")
