#!/usr/bin/env python3
"""Check downloaded Hugging Face files against what the repo says they are (size, and sha256 for LFS files).
  scripts/verify-hf.py REPO REV DIR [--require NAME ...]
    REPO  owner/name        REV  a commit, tag or branch (the one the download used)        DIR  the --local-dir
    --require NAME   a file that must exist (path in the repo); a name ending in / means every file under it
With --require exactly those files are checked (the folder may hold other things); without it, every file that is present and in the repo. hf moves a file into place only once it is complete, so
a short or corrupt file means a bad disk, a manual copy or a download that was killed mid-rename: delete it, re-run the
download. Exit 0 = all good (or the check could not run: offline), 1 = a problem. HF_ENDPOINT overrides the host;
HF_VERIFY_HASH=0 checks sizes only. A file already hashed (same size and mtime) is not hashed again: DIR/.hf-verified.json.
Stdlib only."""
import hashlib, json, os, sys, urllib.parse, urllib.request

STAMP = ".hf-verified.json"


def listing(repo, rev, endpoint):
    """-> {path: {"size": n, "sha256": oid or None}} for every file at rev (follows the API's Link: next pages)"""
    url = f"{endpoint}/api/models/{repo}/tree/{urllib.parse.quote(rev, safe='')}?recursive=true"
    out = {}
    while url:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "local-ai-harness"}), timeout=30) as r:
            for e in json.load(r):
                if e.get("type") == "file":
                    out[e["path"]] = {"size": (e.get("lfs") or {}).get("size", e.get("size")), "sha256": (e.get("lfs") or {}).get("oid")}
            link = r.headers.get("Link") or ""
        url = next((p.split(";")[0].strip(" <>") for p in link.split(",") if 'rel="next"' in p), None)
    return out


def sha256(path, size):
    h, done, step = hashlib.sha256(), 0, 1 << 23
    with open(path, "rb") as f:
        while chunk := f.read(step):
            h.update(chunk)
            done += len(chunk)
            if size > (1 << 30) and sys.stdout.isatty():
                print(f"\r      {done * 100 // size}%", end="", flush=True)
    if size > (1 << 30) and sys.stdout.isatty():
        print("\r      ", end="")
    return h.hexdigest()


def verify(repo, rev, dirpath, require=(), endpoint=None, hashing=True):
    """-> (problems [str], checked int). Raises OSError/ValueError when the listing cannot be had."""
    remote = listing(repo, rev, endpoint or os.environ.get("HF_ENDPOINT", "https://huggingface.co").rstrip("/"))
    problems = []
    want = set()
    for name in require:
        hit = [p for p in remote if p.startswith(name)] if name.endswith("/") else ([name] if name in remote else [])
        if not hit:
            problems.append(f"{name}: not in {repo}@{rev[:12]} (wrong revision?)")
        want.update(hit)
    try:
        stamp = json.load(open(os.path.join(dirpath, STAMP)))
    except (OSError, ValueError):
        stamp = {}
    checked = 0
    for p in sorted(want if require else {p for p in remote if os.path.isfile(os.path.join(dirpath, p))}):
        f = os.path.join(dirpath, p)
        if not os.path.isfile(f):
            problems.append(f"{p}: missing")
            continue
        st, r = os.stat(f), remote[p]
        if r["size"] is not None and st.st_size != r["size"]:
            problems.append(f"{p}: {st.st_size} bytes on disk, {r['size']} expected (cut short? delete it and re-run the download)")
            continue
        checked += 1
        if not (hashing and r["sha256"]):
            continue
        s = stamp.get(p) or {}
        if s.get("size") == st.st_size and s.get("mtime_ns") == st.st_mtime_ns and s.get("sha256") == r["sha256"]:
            continue  # hashed before, untouched since
        if st.st_size > (1 << 28):
            print(f"  verifying {p} ({st.st_size / 2**30:.1f} GiB) ...", flush=True)
        got = sha256(f, st.st_size)
        if got != r["sha256"]:
            problems.append(f"{p}: sha256 {got[:12]}... does not match the repo's {r['sha256'][:12]}... (corrupt: delete it and re-run the download)")
            stamp.pop(p, None)
        else:
            stamp[p] = {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "sha256": got}
    try:
        with open(os.path.join(dirpath, STAMP), "w") as fh:
            json.dump(stamp, fh)
    except OSError:
        pass  # a read-only folder just means no memo
    return problems, checked


def main(argv):
    a = [x for x in argv]
    if len(a) < 3 or a[0] in ("-h", "--help"):
        print(__doc__, file=sys.stderr)
        return 2
    repo, rev, d = a[:3]
    req = []
    rest = a[3:]
    if rest:
        if rest[0] != "--require":
            print(__doc__, file=sys.stderr)
            return 2
        req = rest[1:]
    try:
        problems, n = verify(repo, rev, d, req, hashing=os.environ.get("HF_VERIFY_HASH", "1") != "0")
    except (OSError, ValueError) as e:  # offline, rate-limited, a repo that moved: the download itself already succeeded
        print(f"  integrity check skipped: could not read the file list of {repo} ({e})", file=sys.stderr)
        return 0
    if problems:
        print(f"  integrity check FAILED for {repo}@{rev[:12]}:", file=sys.stderr)
        for p in problems:
            print(f"    - {p}", file=sys.stderr)
        return 1
    print(f"  integrity ok: {n} file(s) of {repo} match the repo")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
