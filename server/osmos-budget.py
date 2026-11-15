#!/usr/bin/env python3
"""osmos's GPU budget, remembered so a start skips the sizing (start-osmos.sh calls this).

osmos-api with budget auto sizes the expert budget by building the engine up to 7 times: ~200 s of a ~207 s start
measured here (3 builds of 63-73 s). A start with a budget builds once. So: a sizing start's result is saved
(watch), and the next start reuses it (pick) while the memory free before launch is at least what it was then, less
the margin that sizing left past osmos's 1 GiB headroom. Less free: size again (fit.h's headroom is the safety; a
budget does not check it). Much more free (> MORE_ROOM): size again too, the extra room is decode speed.

  osmos-budget.py pick  CFG OSMOS_DIR   -> prints the config to start (CFG, or a copy with drafted_budget_gib/budget_gib)
                                           and, on stderr, why
  osmos-budget.py watch PID CFG OSMOS_DIR ROOM   -> (background) saves the sized budget once PID's log says it

Off with harness-osmos-budget = auto (./harness config). State: ~/.cache/local-ai-harness/osmos-budget.json."""
import glob, hashlib, json, os, re, sys, time

AGENT = os.environ.get("PI_CODING_AGENT_DIR") or os.path.join(os.environ.get("PI_HOME") or os.path.expanduser("~/.pi"), "agent")
MODE = os.path.join(AGENT, "harness-osmos-budget")  # remember (default) | auto
CACHE = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "local-ai-harness")  # harness.py's LOGDIR
STATE = os.path.join(CACHE, "osmos-budget.json")
RUN = os.path.join(CACHE, "osmos-run.json")
GiB = 1 << 30
HEADROOM = 1 * GiB      # libosmos fit.h FIT_HEADROOM: what a sizing leaves free past the build
RAM_KEPT = 8 * GiB      # fit.h FIT_RAM_FREE: the RAM a sizing keeps free unless osmos.json's fill_gib says another
MORE_ROOM = 4 * GiB     # this much more free than at the sizing: size again rather than leave it unused
SIZED = re.compile(r"the budget sized to this GPU: ([\d.]+) GiB \(([\d.]+) GiB left free")


def mode():
    try:
        return open(MODE).read().strip() or "remember"
    except OSError:
        return "remember"


def room(cfg):
    """What a sizing can fill, measured the way fit_room bounds it: GPU memory free, or RAM available past the kept."""
    gpu = 0
    for d in glob.glob("/sys/class/drm/card*/device"):
        try:
            r = lambda k: int(open(f"{d}/mem_info_{k}").read())  # noqa: E731
            gpu = max(gpu, r("gtt_total") - r("gtt_used") + r("vram_total") - r("vram_used"))
        except OSError:
            continue
    avail = int(re.search(r"MemAvailable:\s+(\d+)", open("/proc/meminfo").read()).group(1)) * 1024
    kept = int(cfg.get("fill_gib", RAM_KEPT / GiB) * GiB)
    return min(gpu, max(0, avail - kept) + HEADROOM)


def key(cfg_path, osmos_dir):
    """The remembered budget is this config's on this build of osmos: weights, context, fill or a rebuild change it."""
    h = hashlib.sha1(open(cfg_path, "rb").read())
    for b in ("osmos-serve", "osmos-api"):
        try:
            st = os.stat(os.path.join(osmos_dir, "bin", b))
            h.update(f"{b}{st.st_size}{st.st_mtime_ns}".encode())
        except OSError:
            pass
    return h.hexdigest()[:16]


def load():
    try:
        return json.load(open(STATE))
    except (OSError, ValueError):
        return {}


def save(s):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + ".new"
    json.dump(s, open(tmp, "w"), indent=1)
    os.replace(tmp, STATE)


def decide(saved, now):
    """-> (budget GiB or None to size, why). saved: {budget_gib, left_gib, room}; now: room in bytes."""
    if not saved:
        return None, "no size remembered for this config yet: sizing (~3 min, once)"
    slack = max(0.0, saved["left_gib"] * GiB - HEADROOM)  # what the sized build left past the headroom
    if now < saved["room"] - slack:
        return None, f"{(saved['room'] - now) / GiB:.1f} GiB less memory free than at the last sizing: sizing again"
    if now > saved["room"] + MORE_ROOM:
        return None, f"{(now - saved['room']) / GiB:.1f} GiB more memory free than at the last sizing: sizing again to use it"
    return saved["budget_gib"], f"the remembered budget {saved['budget_gib']} GiB (sized {saved['when']}; ./harness config: size every start)"


def pick(cfg_path, osmos_dir):
    cfg = json.load(open(cfg_path))
    if mode() == "auto" or cfg.get("budget_gib") or cfg.get("drafted_budget_gib"):
        return cfg_path, None  # sizing asked for, or a budget the user set in osmos.json: theirs
    budget, why = decide(load().get(key(cfg_path, osmos_dir)), room(cfg))
    if budget is None:
        return cfg_path, why
    cfg["drafted_budget_gib" if cfg.get("drafter") else "budget_gib"] = budget
    os.makedirs(os.path.dirname(RUN), exist_ok=True)
    json.dump(cfg, open(RUN, "w"), indent=2)
    return RUN, why


def watch(pid, cfg_path, osmos_dir, room_before):
    """Follow pid's stdout (the log start_engine gives it): a sizing line -> saved; pid gone before ready on a
    remembered budget -> forgotten, so the next start sizes."""
    try:
        log = os.readlink(f"/proc/{pid}/fd/1")
    except OSError:
        return
    if not os.path.isfile(log):
        return  # a terminal or a pipe: nothing to read back
    t0 = time.time()  # the key is taken when saving: the installer path rewrites osmos.json before it execs the api
    while time.time() - t0 < 1800:
        time.sleep(3)
        try:
            text = open(log, errors="replace").read()
        except OSError:
            return
        m = SIZED.search(text)
        if m:
            s = load()
            s[key(cfg_path, osmos_dir)] = {"budget_gib": float(m.group(1)), "left_gib": float(m.group(2)), "room": room_before,
                    "when": time.strftime("%Y-%m-%d %H:%M")}
            return save(s)
        if "the engine is ready" in text:
            return  # started on a remembered budget: nothing new to save
        if not os.path.exists(f"/proc/{pid}"):
            s = load()
            if s.pop(key(cfg_path, osmos_dir), None):
                save(s)  # it died on the remembered budget: size next time
            return


if __name__ == "__main__":
    cmd, args = sys.argv[1], sys.argv[2:]
    if cmd == "pick":
        path, why = pick(*args)
        if why:
            print(f"osmos: {why}", file=sys.stderr)
        print(path)
    elif cmd == "room":
        print(room(json.load(open(args[0]))))
    elif cmd == "watch":
        watch(int(args[0]), args[1], args[2], int(args[3]))
    elif cmd == "test":  # self-check of the decision
        s = {"budget_gib": 95.88, "left_gib": 1.94, "room": 100 * GiB, "when": "x"}
        assert decide(None, 0)[0] is None
        assert decide(s, 100 * GiB)[0] == 95.88
        assert decide(s, int(99.1 * GiB))[0] == 95.88            # within the 0.94 GiB the sizing left spare
        assert decide(s, 99 * GiB)[0] is None                    # past it: size again
        assert decide(s, 105 * GiB)[0] is None                   # much more room: size again to use it
        assert decide({**s, "left_gib": 1.0}, 100 * GiB - 1)[0] is None  # no spare at all: any less is too little
        print("ok")
