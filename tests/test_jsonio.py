"""python3 tests/test_jsonio.py - the shared atomic JSON writer: failures leave the old file alone, permissions survive."""
import json, os, stat, sys, tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "setup"))
import jsonio

d = tempfile.mkdtemp()
p = os.path.join(d, "sub", "models.json")  # parent folder is created

jsonio.put(p, {"a": 1})
assert json.load(open(p)) == {"a": 1} and open(p).read().endswith("}\n")
assert not os.path.exists(p + ".tmp")

# a value json cannot serialise raises before the file is touched
try:
    jsonio.put(p, {"a": object()})
    raise AssertionError("must raise")
except TypeError:
    pass
assert json.load(open(p)) == {"a": 1} and not os.path.exists(p + ".tmp")

# a failure at the very last step (rename) leaves the old file and cleans the temp file up
real_replace = os.replace
os.replace = lambda *a: (_ for _ in ()).throw(OSError("disk gone"))
try:
    jsonio.put(p, {"a": 2})
    raise AssertionError("must raise")
except OSError:
    pass
finally:
    os.replace = real_replace
assert json.load(open(p)) == {"a": 1} and not os.path.exists(p + ".tmp")

# the replaced file keeps its permissions
os.chmod(p, 0o600)
jsonio.put(p, {"a": 3}, indent=1)
assert stat.S_IMODE(os.stat(p).st_mode) == 0o600 and json.load(open(p)) == {"a": 3}

# a stale temp file from an earlier crash does not block the next write
open(p + ".tmp", "w").write("{half")
jsonio.put(p, {"a": 4})
assert json.load(open(p)) == {"a": 4} and not os.path.exists(p + ".tmp")
print("ok")
