"""Atomic JSON config writes, stdlib only. One writer for the wizard, adopt.py (Linux and Windows), mcp.py, the inline Python in
setup.sh and uninstall.sh, and scripts/set-image-limits.py, so none of them can leave a truncated models.json or settings.json behind.
"""
import json, os, stat, tempfile


def put(path, obj, indent=2):
    """Write obj as JSON to path: serialise first (a bad value raises before the file is touched), write a temp file beside it,
    fsync, rename over the original. A crash or an error at any point leaves the old file as it was. The replaced file keeps its
    permissions (a 0600 models.json stays 0600); a new file is 0600 (models.json can hold API keys). The temp file has a unique
    name and starts at 0600, so two writers never share it and its content is never readable by other users."""
    text = json.dumps(obj, indent=indent) + "\n"
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=os.path.basename(path) + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
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
