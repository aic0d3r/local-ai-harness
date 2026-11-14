"""python3 tests/test_verify_hf.py - scripts/verify-hf.py against a mock Hugging Face API: sizes, sha256, --require, paging, memo, offline."""
import hashlib, http.server, importlib.util, json, os, subprocess, sys, tempfile, threading

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "scripts", "verify-hf.py")
spec = importlib.util.spec_from_file_location("verify_hf", SCRIPT)
v = importlib.util.module_from_spec(spec)
spec.loader.exec_module(v)

BIG = b"abcdefgh" * 1000
SMALL = b"hello"
sha = lambda b: hashlib.sha256(b).hexdigest()
TREE = [  # served in two pages (the API pages large repos with a Link header)
    [{"type": "file", "path": "big.hgn", "size": len(BIG), "lfs": {"oid": sha(BIG), "size": len(BIG)}},
     {"type": "directory", "path": "tokenizer"}],
    [{"type": "file", "path": "tokenizer/tok.json", "size": len(SMALL)}],  # not LFS: size only
]


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_GET(self):
        page = 1 if "page=2" in self.path else 0
        if "/api/models/o/r/tree/abc" not in self.path:
            self.send_response(404); self.end_headers(); return
        b = json.dumps(TREE[page]).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(b)))
        if page == 0:
            self.send_header("Link", f'<http://127.0.0.1:{self.server.server_port}/api/models/o/r/tree/abc?recursive=true&page=2>; rel="next"')
        self.end_headers(); self.wfile.write(b)


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
EP = f"http://127.0.0.1:{srv.server_port}"


def fresh():
    d = tempfile.mkdtemp()
    os.makedirs(os.path.join(d, "tokenizer"))
    open(os.path.join(d, "big.hgn"), "wb").write(BIG)
    open(os.path.join(d, "tokenizer", "tok.json"), "wb").write(SMALL)
    return d


run = lambda d, *a, **k: v.verify("o/r", "abc", d, *a, endpoint=EP, **k)

# a good download, both pages of the listing used (tokenizer/ is on page 2)
d = fresh()
p, n = run(d, ["big.hgn", "tokenizer/"])
assert p == [] and n == 2, (p, n)

# same size, one flipped byte: only the hash catches it; sizes-only mode does not
open(os.path.join(d, "big.hgn"), "r+b").write(b"X")
p, _ = run(d, ["big.hgn"])
assert len(p) == 1 and "sha256" in p[0] and "corrupt" in p[0], p
d2 = fresh(); open(os.path.join(d2, "big.hgn"), "r+b").write(b"X")
assert run(d2, ["big.hgn"], hashing=False)[0] == []

# cut short, missing required, a required directory with nothing in it, a name the repo does not have
d = fresh()
open(os.path.join(d, "big.hgn"), "wb").write(BIG[:100])
assert "100 bytes on disk" in run(d, ["big.hgn"])[0][0]
os.remove(os.path.join(d, "tokenizer", "tok.json"))
p, _ = run(d, ["tokenizer/"])
assert p == ["tokenizer/tok.json: missing"], p  # --require checks exactly what was asked: the short big.hgn is not part of it
assert len(run(d, ["big.hgn", "tokenizer/"])[0]) == 2  # asked for both: both reported
assert "not in o/r@abc" in run(d, ["nope.bin"])[0][0]

# without --require every file that is present and in the repo is checked (a folder that also holds other things)
d = fresh(); open(os.path.join(d, "unrelated.txt"), "w").write("x")
p, n = run(d)
assert p == [] and n == 2

# the memo: a verified file is not hashed again until it changes
d = fresh(); calls = []
real = v.sha256
v.sha256 = lambda path, size: (calls.append(path), real(path, size))[1]
run(d, ["big.hgn"]); run(d, ["big.hgn"])
assert len(calls) == 1, calls
os.utime(os.path.join(d, "big.hgn"), (1, 1))  # touched: hash again
run(d, ["big.hgn"])
assert len(calls) == 2, calls
v.sha256 = real

# the command line: exit 0 + a note when the API cannot be reached, 1 + the reasons when a file is bad, 2 on bad usage
d = fresh()
env = dict(os.environ, HF_ENDPOINT="http://127.0.0.1:9")
r = subprocess.run([sys.executable, SCRIPT, "o/r", "abc", d, "--require", "big.hgn"], env=env, capture_output=True, text=True)
assert r.returncode == 0 and "skipped" in r.stderr, (r.returncode, r.stderr)
open(os.path.join(d, "big.hgn"), "r+b").write(b"X")
r = subprocess.run([sys.executable, SCRIPT, "o/r", "abc", d, "--require", "big.hgn"], env=dict(os.environ, HF_ENDPOINT=EP), capture_output=True, text=True)
assert r.returncode == 1 and "FAILED" in r.stderr and "big.hgn" in r.stderr, (r.returncode, r.stderr)
assert subprocess.run([sys.executable, SCRIPT], capture_output=True).returncode == 2
print("ok")
