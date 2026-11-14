"""Atomic JSON config writes, stdlib only. One writer for the wizard, adopt.py (Linux and Windows), mcp.py, the inline Python in
setup.sh and uninstall.sh, and scripts/set-image-limits.py, so none of them can leave a truncated models.json or settings.json behind.
"""
import json, os, stat


def put(path, obj, indent=2):
    """Write obj as JSON to path: serialise first (a bad value raises before the file is touched), write a temp file beside it,
    fsync, rename over the original. A crash or an error at any point leaves the old file as it was. The replaced file keeps its
    permissions (a 0600 models.json stays 0600); a new file gets the umask default."""
    text = json.dumps(obj, indent=indent) + "\n"
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(tmp, stat.S_IMODE(os.stat(path).st_mode))
        except FileNotFoundError:
            pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
