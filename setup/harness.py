#!/usr/bin/env python3
"""harness - detect the local engine/model, apply optimized pi defaults, switch later.

  ./harness setup [--yes] [--no-tune]   wizard: detect -> ask only if ambiguous -> apply -> tune -> verify
  ./harness use <engine>[/<model>]      switch the active engine/model (no restart of servers you started)
  ./harness tune                        re-run the 1-2 min probe for the active profile
  ./harness status                      what is active, is it answering, last tuning
  ./harness up                          start.sh's first step: saved engine up, install refreshed, update notice
  ./harness verify [engine] [model]     ask the engine for a live reply over HTTP; exit 0 only if it answered
  ./harness refresh                     re-run the installer quietly for the saved engine (what /harness-update runs)
  ./harness update-check [engine]       one line if the active engine has a newer release (when the check is on)
  ./harness config                      change one saved answer (sidecar, voice, update check, engine, sudo hints)
  ./harness detect                      print what was found (json)
  ./harness monitor [engine] [--url U]  live dashboard: Strata's own `strata monitor`, halogen-top.sh, else per-reply speeds pi logs
  ./harness recommend [--json]          best engine, weights quant and context for this hardware, and why

Profiles live in config/profiles.json. Stdlib only. Never kills or restarts servers it did not launch.
"""
import json, os, re, shutil, subprocess, sys, textwrap, time, urllib.parse, urllib.request, uuid, hashlib, glob
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import entries
import jsonio
import strata_doctor

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENT = os.environ.get("PI_CODING_AGENT_DIR") or os.path.join(os.environ.get("PI_HOME", os.path.expanduser("~/.pi")), "agent")
PROFILES = json.load(open(os.path.join(REPO, "config", "profiles.json")))
LOGDIR = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "local-ai-harness")  # not /tmp: a planted symlink there would be followed
STRATA_DATA = os.path.join(LOGDIR, "strata-data")  # Strata's Docker /data: packs, settings, the API key


def strata_key():
    """The bearer token Strata's Docker image serves with (docs/DOCKER.md: always a key); server/start-strata.sh drops it in the volume."""
    try:
        return open(os.path.join(os.environ.get("STRATA_DATA") or STRATA_DATA, "harness-key")).read().strip() or None
    except OSError:
        return None


def engine_key(engine):
    """The token an engine wants, or None when it serves without one."""
    return strata_key() if engine == "strata" else None
MODEL_DIRS = ["~/models", "~/.cache/huggingface/hub", "/models", "~/llm"]
TUNE_BUDGET_S = 120


def jget(path, default=None):
    """The parsed file, or default when it is missing. A file that exists but is not JSON stops the run: the
    callers read-modify-write, and falling back to the default would overwrite the user's file with only our keys."""
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except ValueError as e:
        sys.exit(f"! {path} is not valid JSON ({e}); fix or remove it, then re-run. Nothing was changed.")
    except OSError as e:  # unreadable is not missing: the default would be written over the user's file
        sys.exit(f"! cannot read {path} ({e}); fix its owner/permissions, then re-run. Nothing was changed.")


def jput(path, obj):
    jsonio.put(path, obj)  # temp + rename: a crash mid-write must not leave a truncated config


def tool(name, *a, timeout=None):
    """Run docker / systemctl / pkill, system tools a supported box may not have (Ollama, vLLM, LM Studio and osmos run
    without docker): exit 127 and no output there, where a bare run raised FileNotFoundError out of ./harness status/up."""
    return subprocess.run([name, *a], capture_output=True, text=True, timeout=timeout) if shutil.which(name) else subprocess.CompletedProcess((), 127, "", "")


def http(url, body=None, timeout=3, key=None):
    if not key:  # Strata in Docker serves only with its key: every probe of a local engine port has to carry it
        u = urllib.parse.urlsplit(url)
        if u.hostname in ("127.0.0.1", "localhost"):
            key = next(filter(None, (engine_key(e) for e, p in PROFILES["engines"].items() if u.port == p["port"])), None)
    headers = {"Content-Type": "application/json", **({"Authorization": f"Bearer {key}"} if key else {})}  # key: a custom endpoint's
    return urllib.request.urlopen(urllib.request.Request(url, data=json.dumps(body).encode() if body else None, headers=headers), timeout=timeout)


# ---------- detect ----------
def hardware():
    mem = 0
    try:
        mem = int(re.search(r"MemTotal:\s+(\d+)", open("/proc/meminfo").read()).group(1)) // 1048576
    except Exception:
        pass
    gpu = "none"
    if shutil.which("nvidia-smi"):
        gpu = "nvidia"
    elif os.path.exists("/dev/kfd"):
        gpu = "amd"
    elif sys.platform == "darwin":
        gpu = "apple"
    return {"cpus": os.cpu_count(), "ram_gb": mem, "gpu": gpu, "npu": bool(glob.glob("/dev/accel/accel*"))}


# ---------- recommend: hardware -> engine, weights quant, context ----------
PICK = os.path.join(AGENT, "harness-pick.json")    # accepted recommendation: {engine, quant, ctx}; start_engine passes ctx as GUFO_CTX
MODEL_FILE = os.path.join(AGENT, "harness-model")  # catalog id; the gufo and Strata launchers read it
DEFAULT_MODEL = "flash-next"
ABLITERATED = (DEFAULT_MODEL, "qwen38-27b", "qwen38-27b-q8")  # models with an abliterated build the launchers load (27B: huihui-ai's)
QUANT_FILE = os.path.join(AGENT, "harness-quant")  # stock quant gufo loads; server/start-gufo-flashnext.sh reads it
DEFAULT_QUANT = "UD-Q4_K_XL"


def capacity():
    """What hardware() leaves out. fast_gb: memory the GPU reads at full speed (NVIDIA VRAM; AMD VRAM+GTT, the whole
    shared pool on an APU; Apple unified RAM). Best effort: a failed probe gives 0/"" and the recommendation says so."""
    h = hardware()
    name, vram, fast = "", 0, float(h["ram_gb"])
    try:
        if h["gpu"] == "nvidia":
            row = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"], capture_output=True, text=True).stdout.splitlines()[0]
            name, _, mib = row.rpartition(",")
            name, vram = name.strip(), (int(mib) + 512) // 1024  # a "24 GB" card reports 24564 MiB
            fast = float(vram)
        elif h["gpu"] == "amd":
            pools = [sum(int(open(f"{c}/mem_info_{k}_total").read()) for k in ("vram", "gtt")) / 2**30 for c in glob.glob("/sys/class/drm/card[0-9]/device") if os.path.exists(f"{c}/mem_info_gtt_total")]
            fast = max(pools) if pools else 0.0
            vram = max([int(open(f).read()) / 2**30 for f in glob.glob("/sys/class/drm/card[0-9]/device/mem_info_vram_total")] or [0])
            try:  # pciutils is not installed everywhere; the name is a label, the sizes above do not need it
                lines = [l for l in subprocess.run(["lspci"], capture_output=True, text=True).stdout.splitlines() if re.search(r"VGA|Display|3D", l) and "AMD" in l]
                name = re.sub(r"\s*(\[[^\]]*\]|\(rev [^)]*\))", "", lines[0].split("[AMD/ATI]", 1)[-1].split(": ", 1)[-1]).strip() if lines else ""
            except OSError:
                name = ""
            # Strix Halo is gfx1151 (KFD gfx_target_version 110501): recognised from the kernel even without lspci or a new enough pci.ids
            if "Strix Halo" not in name and any("gfx_target_version 110501" in open(p).read() for p in glob.glob("/sys/class/kfd/kfd/topology/nodes/*/properties")):
                name = "Strix Halo"
            name = name or "AMD GPU"
    except Exception:
        pass
    d = os.environ.get("ENGINE_DIR") or next((p for p in map(os.path.expanduser, ("~/LLMBench/models", "~/models")) if os.path.isdir(p)), os.path.expanduser("~/models"))
    while d != os.path.dirname(d) and not os.path.isdir(d):
        d = os.path.dirname(d)
    return {"gpu_name": name, "vram_gb": round(vram, 1), "fast_gb": round(fast, 1), "disk_free_gb": shutil.disk_usage(d).free // 2**30}


def ename(e):
    """What menus and messages call an engine (profiles.json name: llamacpp is strix-llama.cpp); the id stays what files and pi use."""
    return PROFILES["engines"].get(e or "", {}).get("name", e)


def catalog():
    """{id: entry} of the models the wizard can set up (profiles.json "catalog", minus its _doc)."""
    return {k: v for k, v in PROFILES["catalog"].items() if not k.startswith("_")}


def model_pref():
    try:
        return open(MODEL_FILE).read().strip() or DEFAULT_MODEL
    except OSError:
        return DEFAULT_MODEL


def osmos_inkling():
    """osmos serves its inkling-2-mini pack (server/start-osmos.sh reads the same saved answer), not DeepSeek; stock only."""
    return engine_model("osmos") == "inkling2-mini"


def engine_model(engine):
    """The model the saved answer means for this engine: the answer when this engine can load it, else Flash-Next
    (halogen always serves its own Flash-Next checkpoint)."""
    m = model_pref()
    return m if engine in catalog().get(m, {}).get("engines", ()) else DEFAULT_MODEL


def ctx_for_spare(spare):
    """Context the spare memory (GB left after weights and extras) buys, or None when it is under the smallest tier."""
    return next((t["ctx"] for t in PROFILES["recommend"]["ctx_tiers"] if spare >= t["spare_gb"]), None)


def gufo_ctx(quant, cap):
    """Context a gufo launch on this hardware gets for a stock quant: the same table as recommend(). The smallest tier when
    even that does not fit (the launch then fails loudly rather than being refused here); the model maximum when the quant is unknown."""
    q = next((q for q in PROFILES["recommend"]["quants"] if q["name"] == quant), None)
    tiers = PROFILES["recommend"]["ctx_tiers"]
    if not q or not cap["fast_gb"]:
        return tiers[0]["ctx"]
    return ctx_for_spare(cap["fast_gb"] - q["gib"] - PROFILES["recommend"]["extras_gb"]) or tiers[-1]["ctx"]


def recommend(hw, cap):
    """-> {engine|None, model, quant, ctx, kv, tested, download_gb, why}. Table in config/profiles.json ("recommend" and
    "catalog"). Only halogen on a Strix Halo with ~124 GB follows a measurement; the rest is arithmetic on published
    shard sizes or Strata's own docs and says tested=False. Context is a heuristic on spare memory (KV size per token is
    unmeasured). gufo targets Strix Halo (gfx1151) only; every other AMD/NVIDIA box goes to Strata."""
    R = PROFILES["recommend"]
    fast, ram, vram = cap["fast_gb"], hw["ram_gb"], cap["vram_gb"]
    out = lambda **kw: {"engine": None, "model": None, "quant": None, "ctx": None, "kv": None, "tested": False, "download_gb": 0, **kw}
    none = lambda why: out(why=why)
    strix = "Strix Halo" in cap.get("gpu_name", "")
    if hw["gpu"] == "amd" and strix:
        if hw["npu"] and ram >= R["halogen_min_ram_gb"]:
            return out(engine="halogen", model=DEFAULT_MODEL, tested=True, download_gb=R["halogen_download_gb"],
                       why=f"AMD NPU + GPU with {ram} GB shared memory: halogen runs the whole model and the NPU guard/triage/dedup tools (its launcher sets context and KV slots)")
        for q in R["quants"]:
            spare = fast - q["gib"] - R["extras_gb"]
            ctx = ctx_for_spare(spare)
            if ctx:
                return out(engine="gufo", model=DEFAULT_MODEL, quant=q["name"], ctx=ctx, download_gb=int(q["gib"]) + R["extras_gb"] + 1,
                           why=f"Strix Halo, {fast:.0f} GB addressable: {q['name']} ({q['gib']} GiB) leaves {spare:.0f} GB for context -> {ctx} tokens (gufo has no KV-cache flag)")
        return none(f"Strix Halo with {fast:.0f} GB addressable: the smallest quant ({R['quants'][-1]['gib']} GiB) plus context does not fit")
    if hw["gpu"] in ("amd", "nvidia"):
        cat = catalog()
        # Strata does not state a VRAM minimum; the smallest card its published speeds list is 12 GB. Say so, do not refuse (0 = probe failed)
        small = f" Only {vram:g} GB VRAM: Strata's published speeds start at {R['strata_listed_vram_gb']} GB cards, so expect it to be slow." if 0 < vram < R["strata_listed_vram_gb"] else ""
        if weights_pref() == "abliterated":  # only the Unsloth-layout Flash-Next has an abliterated build
            if ram + vram < R["strata_min_gb"]:
                return none(f"{vram} GB VRAM + {ram} GB RAM: Strata's UD-Q4_K_XL (the abliterated weights) needs ~{R['strata_min_gb']} GB of RAM and VRAM together to keep every expert in memory (Strata's figure)")
            return out(engine="strata", model=DEFAULT_MODEL, download_gb=quant_gb(DEFAULT_QUANT),
                       why=f"{vram} GB VRAM + {ram} GB RAM: Strata runs the abliterated Flash-Next you chose (UD-Q4_K_XL layout).{small}")
        for mid in R["strata_order"]:  # the biggest pack whose RAM need (experts + ~10 GB, Strata's rule) fits
            if ram >= cat[mid]["ram_gb"]:
                return out(engine="strata", model=mid, download_gb=cat[mid]["download_gb"],
                           why=f"{vram} GB VRAM + {ram} GB RAM: {cat[mid]['label']} needs ~{cat[mid]['ram_gb']} GB RAM; Strata keeps what does not fit the card in RAM (and runs on Windows and Mac too).{small}")
        return none(f"{ram} GB RAM is under the {cat[R['strata_order'][-1]]['ram_gb']} GB the smallest pack needs")
    return none(f"no supported GPU path on this box (gpu={hw['gpu']}); run Strata natively there and point pi at it: ./setup.sh --strata <url>")


def serves_other(engine, running):
    """gufo is up on another model than the saved answer (its served names are ours: qwen3.8-27b-q8 -> qwen38-27b-q8).
    Only gufo: Strata's served ids are not ours to map."""
    served = running.get(engine, {}).get("models") or []
    return engine == "gufo" and bool(served) and catalog_id(served[0]) != engine_model(engine)


def best_engine(route, hw):
    """First engine of a profiles.json route this box can run: -> {engine, why, ...} or None."""
    ok = lambda e: e.get("needs") != "npu" or (hw["npu"] and hw["ram_gb"] >= PROFILES["recommend"]["halogen_min_ram_gb"])
    return next((e for e in route["engines"] if ok(e)), None)


ENGINE_MENU = object()  # ask_route's last row: the user wants the engine menu instead


def ask_route(esc=None):
    """Strix Halo: ask which MODEL to run and pick its best engine (profiles.json \"routes\"). Saves the model answer.
    -> (engine, model), None when the box is not a Strix Halo / there is no terminal, ENGINE_MENU when the user wants
    the engine instead, or esc (BACK) for Esc. Esc in the quant menu comes back here."""
    if not sys.stdin.isatty() or "Strix Halo" not in capacity().get("gpu_name", ""):
        return None
    hw, routes = hardware(), {k: v for k, v in PROFILES["routes"].items() if not k.startswith("_")}
    other = "Pick an engine instead"
    cur = (active() or {}).get("engine")
    rows = [(k, b, route_speed(b, r["model"])) for k, r in routes.items() for b in [best_engine(r, hw)] if b]
    w = [max(len(c) for c in col) for col in zip(*[(routes[k]["label"].split(" (")[0], routes[k].get("short", ""), ename(b["engine"])) for k, b, _ in rows])]
    # routes are in preference order: the first row is the recommendation (said in the detail: "(recommended)" pushed rows past 80 columns)
    labels = {f"{routes[k]['label'].split(' (')[0]:<{w[0]}}  {routes[k].get('short', ''):<{w[1]}}  {ename(b['engine']):<{w[2]}}  {s}"
              + ("  (current)" if b["engine"] == cur and engine_model(cur).startswith(routes[k]["model"] or "") else ""): (k, b)
              for k, b, s in rows}
    detail = "the first is recommended; each runs on its fastest engine here; t/s = decode speed" + (
        ", ~ = published, not yet measured on this box" if any(s.startswith("~") for *_, s in rows) else "")
    while True:
        pick = choose(f"Which model do you want to run? {detail}", [*labels, other], False, esc)
        if pick is BACK:
            return BACK
        if pick == other:
            return ENGINE_MENU
        k, b = labels[pick]
        model = routes[k]["model"]
        if model:
            model = ask_variant(model, b["engine"], BACK)
            if model is BACK:
                continue
        return b["engine"], model


def ask_variant(model, engine, esc=None):
    """A route's model has quants the engine loads as their own catalog ids (qwen38-27b, qwen38-27b-q8): ask which, the saved
    one first, and save it. One variant: keep a saved quant of the same family (flash-next-iq3_s) or save the route's model."""
    cat, cur = catalog(), model_pref()
    ids = [m for m in cat if (m == model or m.startswith(model + "-")) and engine in cat[m]["engines"]]
    if len(ids) > 1:
        ids.sort(key=lambda m: m != cur)
        pick = {cat[m]["label"].split(" (")[0]: m for m in ids}
        notes = [f"{n}: {cat[m]['label'].partition(' (')[2].rstrip(')')}, ~{cat[m]['download_gb']} GB download" for n, m in pick.items()]
        choice = choose(f"Which {routes_label(model)} for {ename(engine)}?", list(pick), False, esc, notes)
        if choice is BACK:
            return BACK
        model = pick[choice]
    elif cur.startswith(model):
        return model
    if model != cur:
        os.makedirs(AGENT, exist_ok=True)
        open(MODEL_FILE, "w").write(model + "\n")
    return model


def routes_label(model):
    return next((r["label"].split(" (")[0] for k, r in PROFILES["routes"].items() if not k.startswith("_") and r.get("model") == model), model)


def route_speed(b, model):
    """Decode speed for a model menu row: this box's ./harness tune result for the saved variant and weights, else the published one with ~."""
    if model and model_pref().startswith(model):  # the saved variant (27B Q8), not the route's first one
        model = model_pref()
    got = speeds(b["engine"], model)
    v = got.get(weights_pref()) or got.get("stock") or next(iter(got.values()), None)  # the weights you run first
    if v:
        return f"{v['decode_tps']:g} t/s"
    m = re.search(r"~?[\d.]+(?:-[\d.]+)? t/s", b["why"])
    return "~" + m.group().lstrip("~") if m else ""


def describe(rec, cap):
    gpu = f"{cap['gpu_name'] or 'no GPU'}, {cap['fast_gb']:g} GB GPU-addressable, {cap['disk_free_gb']} GB disk free"
    if not rec["engine"]:
        return [f"hardware: {gpu}", f"recommended: nothing - {rec['why']}"]
    label = catalog().get(rec["model"], {}).get("label") if rec["model"] and rec["model"] != DEFAULT_MODEL else None
    out = [f"hardware: {gpu}",
           f"recommended: {ename(rec['engine'])}" + (f", model {label}" if label else "") + (f", weights {rec['quant']}" if rec["quant"] else "") + (f", context {rec['ctx']}" if rec["ctx"] else "") + (f", {rec['kv']} KV cache" if rec["kv"] else "")
           + ("" if rec["tested"] else "  (untested on this class of hardware)"), f"  why: {rec['why']}"]
    if rec["download_gb"] and cap["disk_free_gb"] < rec["download_gb"] + 5:
        out.append(f"  ! a fresh download is ~{rec['download_gb']} GB, only {cap['disk_free_gb']} GB free (fine if the weights are already on disk)")
    return out


def quant_gb(name):
    """Download size in GB for a stock quant: shards + MTP head + projector. None for an unknown quant."""
    q = next((q for q in PROFILES["recommend"]["quants"] if q["name"] == name), None)
    return q and int(q["gib"]) + PROFILES["recommend"]["extras_gb"] + 1


def saved_quant(engine="gufo"):
    """The stock quant the engine loads: Strata only knows the default; gufo follows the saved hardware pick."""
    try:
        q = open(QUANT_FILE).read().strip()
    except OSError:
        q = ""
    return q if engine == "gufo" and quant_gb(q) else DEFAULT_QUANT


def offer_recommendation(eng):
    """Right after the engine pick: when it is the recommended one, offer its model, quant and context and save the
    answer. Never changes the engine and never asks twice for the same settings. -> True when the user accepted
    (the model question is then already answered)."""
    if not sys.stdin.isatty():
        return False
    rec = recommend(hardware(), capacity())
    if rec["engine"] != eng or eng not in ("gufo", "strata"):  # halogen sizes itself
        return False
    want = {"engine": eng, "model": rec["model"], "quant": rec["quant"], "ctx": rec["ctx"]}
    if jget(PICK, None) == want and engine_model(eng) == rec["model"] and (eng != "gufo" or saved_quant() == rec["quant"]):
        return True
    what = catalog()[rec["model"]]["label"] + (f", {rec['quant']}" if rec["quant"] else "") + (f", context {rec['ctx']}" if rec["ctx"] else "")
    if not ask_yn(f"Use the recommended settings? {what}; {rec['why']}", True):
        return False
    os.makedirs(AGENT, exist_ok=True)
    open(MODEL_FILE, "w").write(rec["model"] + "\n")
    if eng == "gufo" and rec["quant"]:
        open(QUANT_FILE, "w").write(rec["quant"] + "\n")
    jput(PICK, want)
    return True


def ask_model(engine, back=False):
    """Which model the engine loads, when it can load more than one; current answer first. -> True if it changed, or BACK."""
    cat = catalog()
    ids = [m for m in cat if engine in cat[m]["engines"]]
    if len(ids) < 2 or not sys.stdin.isatty():
        return False
    cur = engine_model(engine)
    ids.sort(key=lambda m: m != cur)
    pick, notes = {}, []
    for m in ids:  # the label's (aside) moves to a note, so the row fits a window and gum keeps the arrow menu
        name, _, aside = cat[m]["label"].partition(" (")
        pick[name + (f"  ~{cat[m]['download_gb']} GB download, ~{cat[m]['ram_gb']} GB RAM" if "ram_gb" in cat[m] else "") + ("  (current)" if m == cur else "")] = m
        if aside:
            notes.append(f"{name}: {aside.rstrip(')')}")
    choice = choose(f"Which model for {ename(engine)}?", list(pick), False, BACK if back else None, notes)
    if choice is BACK:
        return BACK
    m = pick[choice]
    os.makedirs(AGENT, exist_ok=True)
    open(MODEL_FILE, "w").write(m + "\n")
    return m != cur


def served_models(port, engine=None, base=None, key=None):
    """Model ids the server lists, minus helper models the engine hides (halogen's NPU sidecars). base/key: a custom endpoint's."""
    hide = PROFILES["engines"].get(engine or "", {}).get("hide")
    key = key or engine_key(engine or "")
    try:
        ids = [m["id"] for m in json.load(http(f"{base or f'http://127.0.0.1:{port}'}/v1/models", key=key))["data"]]
    except (OSError, ValueError, LookupError):
        return []
    return [i for i in ids if not (hide and re.search(hide, i))]


def answers_ready(port, path):
    """A server that lists models before it can answer (osmos loads 91 GiB first) is not up yet: its ready path says."""
    try:
        return b"ok" in http(f"http://127.0.0.1:{port}{path}", timeout=2).read().lower()
    except Exception:
        return False


def strix_container(port):
    """True when server/start-qwen36.sh's container (llamacpp-<port>) is up: strix-llama.cpp, not a llama-server of your own."""
    try:
        return bool(tool("docker", "ps", "-q", "--filter", f"name=^llamacpp-{port}$", timeout=10).stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return False


def identify(name, port):
    """Several engines share :8080 (strata / strix-llama.cpp / llama.cpp / gufo): tell them apart by what answers."""
    if name in ("llamacpp", "llama-server", "gufo"):  # llama.cpp tags its models owned_by=llamacpp, gufo owned_by=gufo; Strata also serves /props, so that is no tell
        try:
            owner = any(m.get("owned_by") == ("gufo" if name == "gufo" else "llamacpp") for m in json.load(http(f"http://127.0.0.1:{port}/v1/models"))["data"])
        except (OSError, ValueError, LookupError):
            return False
        return owner if name == "gufo" or not owner else strix_container(port) == (name == "llamacpp")
    if name == "strata":
        return not identify("llamacpp", port) and not identify("llama-server", port) and not identify("gufo", port)
    return True


def running_engines():
    found = []
    for name, e in PROFILES["engines"].items():
        try:
            http(f"http://127.0.0.1:{e['port']}{e['probe']}")
        except Exception:
            continue
        if identify(name, e["port"]):
            found.append({"engine": name, "port": e["port"], "models": served_models(e["port"], name)})
    return found


def gtt_gb():
    """GPU memory in use (GiB): what decides whether the next engine fits."""
    return sum(int(open(f).read()) for f in glob.glob("/sys/class/drm/card*/device/mem_info_gtt_used")) / 2**30


def mem_pools():
    """Bytes. unified: the GPU has no memory of its own (APU, Strix Halo; KFD local_mem_size 0), so GTT and the BIOS
    carve-out (vram) both come out of system RAM; on a separate card vram is its own pool.
    shortcut: AMD only, NVIDIA cards show RAM alone; add nvidia-smi when someone runs one."""
    mi = {l.split(":")[0]: int(l.split()[1]) * 1024 for l in open("/proc/meminfo")}
    cards = [c for c in glob.glob("/sys/class/drm/card[0-9]/device") if os.path.exists(f"{c}/mem_info_gtt_used")]
    rd = lambda k: sum(int(open(f"{c}/mem_info_{k}").read()) for c in cards)
    gpus = [open(p).read() for p in glob.glob("/sys/class/kfd/kfd/topology/nodes/*/properties")]
    gpus = [p for p in gpus if not re.search(r"^simd_count 0$", p, re.M)]
    return {"gpu": bool(cards), "unified": bool(gpus) and all(re.search(r"^local_mem_size 0$", p, re.M) for p in gpus),
            "ram_total": mi["MemTotal"], "ram_free": mi["MemAvailable"],
            "vram_total": rd("vram_total") if cards else 0, "vram_used": rd("vram_used") if cards else 0, "gtt_used": rd("gtt_used") if cards else 0}


def proc_mem(pid):
    """[ram, gtt, vram] bytes one process holds; gtt/vram None when its fds are not ours to read (a root container).
    ram is anonymous + shared memory: an mmapped model file is page cache the kernel gives back anyway."""
    try:
        st = {l.split(":")[0]: int(l.split()[1]) * 1024 for l in open(f"/proc/{pid}/status") if l.startswith(("RssAnon", "RssShmem"))}
    except OSError:
        return [0, 0, 0]
    out, seen = [sum(st.values()), 0, 0], set()
    try:
        fds = os.listdir(f"/proc/{pid}/fd")
    except OSError:
        return [out[0], None, None]
    for fd in fds:
        try:
            if not os.readlink(f"/proc/{pid}/fd/{fd}").startswith("/dev/dri/"):
                continue
            kv = dict(l.split(":", 1) for l in open(f"/proc/{pid}/fdinfo/{fd}") if ":" in l)
        except OSError:
            continue  # closed meanwhile
        cid = (kv.get("drm-pdev", "").strip(), kv.get("drm-client-id", fd).strip())
        if cid in seen:  # one GPU client open on several fds counts once
            continue
        seen.add(cid)
        for i, k in ((1, "gtt"), (2, "vram")):
            v = kv.get(f"drm-total-{k}", kv.get(f"drm-memory-{k}", "0 KiB")).split()
            out[i] += int(v[0]) * {"KiB": 1024, "MiB": 2**20, "GiB": 2**30}.get(v[1] if len(v) > 1 else "", 1)
    return out


def port_pids(port, engine=None):
    """Host pids serving port: a container's whole process list, else the listener (ours only) and its children."""
    cids = engine_containers(engine) if engine else tool("docker", "ps", "-q", "--filter", f"publish={port}").stdout.split()
    if cids:
        return {int(p) for c in cids for p in tool("docker", "top", c, "-eo", "pid").stdout.split()[1:]}
    pids = {int(p) for p in re.findall(r"pid=(\d+)", subprocess.run(["ss", "-ltnpH", f"sport = :{port}"], capture_output=True, text=True).stdout)}
    parent = {}
    for s in glob.glob("/proc/[0-9]*/stat"):
        try:
            parent[int(s.split("/")[2])] = int(open(s).read().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            pass
    while True:  # osmos-api starts osmos-serve, which holds the memory
        more = {p for p, pp in parent.items() if pp in pids} - pids
        if not more:
            return pids
        pids |= more


def mem_usage(live, sidecar):
    """[(label, [ram, gtt, vram], estimated)] for each running engine (live: engine -> port) and the sidecar.
    A root container's GPU memory cannot be read without sudo: what readable processes do not account for is given to
    those engines, one combined entry when there are several."""
    m = mem_pools()
    rows = [(ename(e), port_pids(port, e)) for e, port in live.items()] + ([("sidecar", port_pids(8090))] if sidecar else [])
    rows = [(n, [proc_mem(p) for p in pids]) for n, pids in rows]
    known = [n for n, ps in rows if all(p[1] is not None for p in ps)]
    out = [(n, [sum(p[i] for p in ps) for i in range(3)], False) for n, ps in rows if n in known]
    blind = [(n, ps) for n, ps in rows if n not in known]
    if blind:
        mine = [proc_mem(p) for p in own_pids()] if m["gpu"] else []
        left = [max(0, m[k] - sum(p[i] or 0 for p in mine)) for i, k in ((1, "gtt_used"), (2, "vram_used"))]
        out.append((" + ".join(n for n, _ in blind), [sum(p[0] for _, ps in blind for p in ps), *left], True))
    return out, m


def own_pids():
    for p in glob.glob("/proc/[0-9]*"):
        try:
            if os.stat(p).st_uid == os.getuid():
                yield int(p.split("/")[2])
        except OSError:
            pass  # exited meanwhile


def mem_line(live, sidecar):
    """'osmos :8091 106 GiB, sidecar 9 GiB  (8 of 121 GiB free' - the caller closes the bracket."""
    use, m = mem_usage(live, sidecar)
    names = {ename(e): f"{ename(e)} :{p}{'' if PROFILES['engines'][e].get('script') else ' (not ours)'}" for e, p in live.items()}
    parts = [f"{' + '.join(names.get(n, n) for n in label.split(' + '))} {'~' if est else ''}{fmt_share(b, m)}" for label, b, est in use]
    return f"{', '.join(parts) or 'nothing'}  ({fmt_free(m)}"


def fmt_share(b, m):
    G = 2**30
    if m["unified"] or not m["gpu"]:
        return f"{sum(b) / G:.0f} GiB"
    return f"{b[2] / G:.0f} GiB VRAM + {(b[0] + b[1]) / G:.0f} GiB RAM"


def mem_after_stop():
    m = mem_pools()
    held = m["gtt_used"] / 2**30
    # >3 GiB GPU memory still held with no engine left = the one stopped leaked it (halogen issue #79)
    leak = held > 3 and not running_engines() and not sidecar_up()
    return fmt_free(m) + (f"; {held:.0f} GiB GPU memory still held with nothing running: reboot before the next engine start" if leak else "")


def fmt_free(m):
    """Room for the next engine. A small carve-out folds into RAM; a large one (BIOS set to 64-96 GiB) is GPU-only room
    a RAM-heavy engine such as osmos cannot use, so it is named apart."""
    G = 2**30
    vfree, vt = (m["vram_total"] - m["vram_used"]) / G, m["vram_total"] / G
    ram = f"{m['ram_free'] / G:.0f} of {m['ram_total'] / G:.0f} GiB"
    if not m["gpu"] or (m["unified"] and vt <= 8):
        return f"{ram} free"
    if m["unified"]:
        return f"{m['ram_free'] / G:.0f} GiB RAM, {vfree:.0f} of {vt:.0f} GiB GPU-reserved free"
    return f"{vfree:.0f}/{vt:.0f} GiB VRAM, {m['ram_free'] / G:.0f}/{m['ram_total'] / G:.0f} GiB RAM free"


def sidecar_up():
    try:
        http("http://127.0.0.1:8090/health")
        return True
    except Exception:
        return False


def stop_sidecar():
    """ling-tiny runs as a user service, or as a bare container / llama-server from an older start (as stop-all.sh stops it)."""
    tool("systemctl", "--user", "stop", "ling-tiny.service")
    tool("docker", "rm", "-f", "ling-tiny")
    tool("pkill", "-f", "--", "-a ling3.0-tiny")


def ggufs():
    """Up to 20 *.gguf under the model folders: what ./harness detect prints. Not part of detect(): a recursive walk of the
    Hugging Face cache on every ./start.sh, for a key nothing else reads."""
    found = []
    for d in MODEL_DIRS:
        found += glob.glob(os.path.join(os.path.expanduser(d), "**", "*.gguf"), recursive=True)[:20]
    return found[:20]


def detect():
    return {"hardware": hardware(), "engines": running_engines(), "docker": bool(shutil.which("docker")), "pi": bool(shutil.which("pi"))}


# ---------- ask ----------
def cancel():
    """Ctrl-C at any prompt, or a plain message when nothing else can be done: leave cleanly, no traceback."""
    print("\ncancelled")
    sys.exit(130)


BACK = object()  # Esc in a submenu: return to the menu above instead of quitting everything


def esc_label(esc):
    """What Esc does here, as the prompt says it."""
    if esc is None:
        return "Esc cancels"
    return "Esc = back" if esc is BACK else f"Esc = {esc}"


def key():
    """One keypress without Enter (the no-gum prompts), so Esc works there too."""
    import termios, tty
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)  # keeps ISIG: Ctrl-C raises KeyboardInterrupt
        data = os.read(fd, 8)  # arrow keys arrive as one read ("\x1b[A"), bare Esc as "\x1b"
        # Ctrl-D (or the terminal went away) and Ctrl-C cancel. Ctrl-C normally arrives as a signal, but not every setup
        # delivers it as one (a pty without a controlling terminal hands it over as a byte): treat both the same.
        if not data or b"\x04" in data or b"\x03" in data:
            cancel()
        return data.decode(errors="ignore")
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def head_detail(question):
    """'Ask this? the rest' -> ('Ask this?', 'the rest'). The headline is the question and stays short; the detail explains it.
    gum truncates a header at the terminal width instead of wrapping, so it only ever gets the headline."""
    i = question.find("?")
    if i < 0:
        return question, ""
    return question[:i + 1], question[i + 1:].strip()


def question_text(question, notes=()):
    """The question as printed: the headline bold, the detail dimmed under it, both wrapped. Each note is its own
    dimmed paragraph under the detail."""
    head, detail = head_detail(question)
    w = term_width()
    out = [paint("1", line) for line in textwrap.wrap(head, w)]
    for para in (detail, *notes):
        out += [paint("2", line) for line in textwrap.wrap(para, w, initial_indent="  ", subsequent_indent="    " if para is not detail else "  ")]
    return "\n".join(out)


def term_width():
    """Columns to wrap to: the terminal's, capped at 78 so a maximized window does not give very long lines. Piped output
    is never wrapped."""
    return min(78, max(20, shutil.get_terminal_size().columns - 2)) if sys.stdout.isatty() else 10**6


def no_terminal(question, answer):
    """A question nobody can answer: the default is taken, and the output says so (a log shows what was chosen)."""
    print(paint("2", f"{question} -> {answer} (no terminal: default)"))


def escape(esc):
    """Esc or Ctrl-C in a menu: cancel the wizard, or return esc when the menu says what Esc means (settings: done)."""
    if esc is None:
        cancel()
    print()  # end the prompt line the way Enter does
    return esc


def gum_hint(esc):
    """gum's own help line says nothing about Esc, so ours replaces it (--no-show-help), above the menu."""
    return paint("2", f"  \u2191\u2193 move \u00b7 Enter picks \u00b7 {esc_label(esc)}")


def fits(options):
    """True when every label fits the terminal: gum cuts a longer one off mid-word instead of wrapping it."""
    w = shutil.get_terminal_size().columns if sys.stdout.isatty() else 200
    return all(len(o) + 2 <= w for o in options)  # +2: gum's cursor prefix


def choose(question, options, yes, esc=None, notes=()):
    """notes: detail lines printed wrapped under the question. A row's detail goes here, not into its label: gum cuts a
    label at the window edge, and a label too wide drops the menu to the numbered list."""
    if len(options) == 1 or yes:
        return options[0]
    if not sys.stdin.isatty():
        no_terminal(question, options[0])
        return options[0]
    if shutil.which("gum") and fits(options):
        print(question_text(question, notes))  # also the fallback: labels too wide for gum wrap here instead of being cut off
        print(gum_hint(esc))
        # stdout only: gum draws its menu on stderr, so capturing stderr leaves the user a blank screen
        r = subprocess.run(["gum", "choose", "--no-show-help", "--header", "", *options], stdout=subprocess.PIPE, text=True)
        out = r.stdout.strip()
        if out:  # gum trims the line it prints: map it back to the exact option (labels can start with a space)
            print(paint("2", f"> {out}"))  # gum clears its menu: keep the answer on screen, as a yes/no does
            return next((o for o in options if o.strip() == out), out)
        if r.returncode == 130:  # Ctrl-C: quit everything, even in a submenu (gum exits 1 for a bare Esc)
            cancel()
        if r.returncode == 1:  # Esc: back one level
            return escape(esc)
    print(question_text(question, notes))
    for i, o in enumerate(options, 1):
        print("\n".join(textwrap.wrap(f"{i}) {o}", term_width(), initial_indent="  ", subsequent_indent="     ")))
    back = esc_label(esc)
    if len(options) <= 9:  # one keypress picks
        prompt = f"pick 1-{len(options)} " + paint("2", f"(Enter = 1, {back})") + ": "
        print(prompt, end="", flush=True)
        while True:
            k = key()
            if k == "\x1b":
                return escape(esc)
            if k in ("\r", "\n") or (k.isdigit() and 0 < int(k) <= len(options)):
                i = int(k) if k.isdigit() else 1
                print(i)
                return options[i - 1]
            # other keys used to vanish silently: say what works, then ask again
            print("\n" + paint("2", f"  type 1-{len(options)}, Enter for 1, or {back.lower()}") + "\n" + prompt, end="", flush=True)
    # more than 9: a whole line, so 10 or 12 can be typed. A typo asks again; it used to pick option 1 silently.
    back2 = back.replace("Esc", "Esc + Enter", 1)  # a line prompt: Esc needs Enter too
    prompt = f"pick 1-{len(options)} " + paint("2", f"(Enter = 1, {back2})") + ": "
    while True:
        a = input(prompt).strip()
        if "\x1b" in a:
            return escape(esc)
        if not a:
            return options[0]
        if a.isdigit() and 0 < int(a) <= len(options):
            return options[int(a) - 1]
        print(paint("2", f"  type 1-{len(options)}, Enter for 1, or {back2}"))


# ---------- profile ----------
def model_profile(model_id):
    return entries.model_profile(model_id, PROFILES)


def server_ctx(port, base=None, key=None):
    """Real context window from the server, or 0 if it will not say. base/key: a custom endpoint's."""
    key = key or next(filter(None, (engine_key(e) for e, p in PROFILES["engines"].items() if p["port"] == port)), None)
    for path, pick in (("/props", lambda d: d["default_generation_settings"]["n_ctx"]), ("/v1/models", lambda d: d["data"][0]["meta"]["n_ctx"]),
                       ("/v1/models", lambda d: d["data"][0]["context_length"]),  # gufo
                       ("/v1/models", lambda d: d["data"][0]["max_model_len"])):  # vLLM
        try:
            return int(pick(json.load(http(f"{base or f'http://127.0.0.1:{port}'}{path}", key=key))))
        except Exception:
            pass
    return 0


def engine_ctx(engine):
    """The window the engine serves: what the server says, else the profile's `ctx` (osmos reads its pack's osmos.json
    and serves 1M without reporting it), else 0 = unknown."""
    return server_ctx(PROFILES["engines"][engine]["port"]) or int(PROFILES["engines"][engine].get("ctx") or 0)


def server_input(port):
    """Input modalities from the running server (gufo >=0.32 serves images via the adjacent mmproj); text-only if it will not say."""
    try:
        mods = json.load(http(f"http://127.0.0.1:{port}/v1/models"))["data"][0].get("architecture", {}).get("input_modalities")
        if mods:
            return mods
    except Exception:
        pass
    return ["text"]


def build_entry(engine, model_id, ctx, tuned):
    e = PROFILES["engines"][engine]
    if not ctx:
        print(f"  ! {ename(engine)} did not report its context window; assuming 32768. If it runs a different size, set contextWindow for {model_id} in models.json")
        ctx = 32768
    p = entries.build_provider(f"http://127.0.0.1:{e['port']}/v1", engine, model_id, ctx, server_input(e["port"]), PROFILES,
                               max_tokens=tuned.get("maxTokens"))
    if engine_key(engine):
        p["apiKey"] = engine_key(engine)  # Strata's Docker image rejects anything else; pi sends it as the bearer token
    return p


def reserve_for(ctx):
    """Compaction reserve for a window: a quarter of it, at most 60k (what the halogen launcher sets at 262k)."""
    return int(min(60000, ctx // 4))


def reserve_is_ours(r, ctx, ours=None):
    """Is the compaction reserve ours to move with the window? Unset, the value we last wrote (harness-profile.json), this
    window's default, or one that no longer fits half the window. A /tune value (/tune writes settings.json only) differs
    from all of those and stays. One rule for sync_ctx and apply(), which used to disagree."""
    return r is None or r == ours or r == reserve_for(ctx) or r >= ctx // 2


def sync_ctx(engine):
    """Keep pi's contextWindow and compaction reserve in step with the window the server really serves (GUFO_CTX, a Strata
    --max-context, a launcher fallback): a stale window makes pi budget against a context the server does not have.
    A reserve we own follows the window; a /tune value that still fits is kept."""
    e = PROFILES["engines"][engine]
    ctx, mid = server_ctx(e["port"]), (active() or {}).get("model")
    mj = os.path.join(AGENT, "models.json")
    m = jget(mj, {})
    ent = next((x for x in m.get("providers", {}).get(e["provider"], {}).get("models", []) if x.get("id") == mid), None)
    if not ctx or not ent:
        return
    if ent.get("contextWindow") != ctx:
        ent["contextWindow"] = ctx
        jput(mj, m)
    sj = os.path.join(AGENT, "settings.json")
    s = jget(sj, {})
    comp = s.setdefault("compaction", {})
    prof = active() or {}
    if reserve_is_ours(comp.get("reserveTokens"), ctx, prof.get("reserveTokens")) and comp.get("reserveTokens") != reserve_for(ctx):
        comp["reserveTokens"] = reserve_for(ctx)
        jput(sj, s)
        prof["reserveTokens"] = reserve_for(ctx)
        jput(os.path.join(AGENT, "harness-profile.json"), prof)


def known_ctx(engine, model_id, old):
    """The server will not say what window it runs (Ollama and LM Studio do not report the loaded one, and load far less than
    the model maximum by default). Keep what models.json already holds for this model, else ask. 0 = unknown: build_entry assumes
    32768 and says so."""
    prev = next((x.get("contextWindow") for x in (old or {}).get("models", []) if x.get("id") == model_id), 0)
    if prev or not sys.stdin.isatty():
        return prev or 0
    hint = {"ollama": "num_ctx, or OLLAMA_CONTEXT_LENGTH", "lmstudio": "the context length the model is loaded with"}.get(engine, "the server's context setting")
    ans = input(f"  {ename(engine)} does not report its context window. What is it set to? ({hint}) [32768] ").strip()
    return int(ans) if ans.isdigit() and int(ans) >= 2048 else 0


def apply(engine, model_id, tuned=None):
    tuned = tuned or {}
    e = PROFILES["engines"][engine]
    mj = os.path.join(AGENT, "models.json")
    m = jget(mj, {})
    if os.path.exists(mj) and not os.path.exists(mj + ".pre-harness"):
        shutil.copy(mj, mj + ".pre-harness")  # one-time backup before we first touch it
    prov = e["provider"]
    old = m.get("providers", {}).get(prov)
    ctx = engine_ctx(engine) or known_ctx(engine, model_id, old)
    new = build_entry(engine, model_id, ctx, tuned)
    if old and engine == "halogen":  # setup.sh provisions the halogen provider; never overwrite it
        new = old
    elif old:  # the user's other models and own key stay; only this model's entry and the URL are ours
        key = new["apiKey"] if engine_key(engine) or old.get("apiKey") in (None, "", "dummy") else old["apiKey"]
        new = {**old, "baseUrl": new["baseUrl"], "api": new["api"], "apiKey": key,
               "models": new["models"] + [x for x in old.get("models", []) if x.get("id") != model_id]}
    m.setdefault("providers", {})[prov] = new
    # compaction/branch-summary look the sidecar up in the model registry; setup.sh only adds it on
    # the halogen path, so without this every other engine silently fell back to main-model summaries
    m["providers"].setdefault("llamacpp-tiny", entries.tiny_provider(PROFILES))
    jput(mj, m)
    ctx = new["models"][0]["contextWindow"]
    sj = os.path.join(AGENT, "settings.json")
    if os.path.exists(sj) and not os.path.exists(sj + ".pre-harness"):
        shutil.copy(sj, sj + ".pre-harness")  # same one-time backup as models.json
    s = jget(sj, {})
    s["defaultProvider"], s["defaultModel"] = prov, new["models"][0]["id"]
    reserve = reserve_for(ctx)  # not tuned["reserveTokens"]: a cached probe may be from another window (gufo moved 65k -> 262k)
    comp = s.setdefault("compaction", {})
    comp.setdefault("enabled", True)  # a user who turned compaction off meant it
    # a /tune reserve that still fits is kept, as sync_ctx keeps it; the profile remembers only what we wrote (the marker of "ours")
    prof = {"engine": engine, "provider": prov, "model": new["models"][0]["id"], "npu": bool(e["npu"]),
            "npuUrl": "http://127.0.0.1:8731", "sidecarUrl": "http://127.0.0.1:8090"}
    if reserve_is_ours(comp.get("reserveTokens"), ctx, (active() or {}).get("reserveTokens")):
        comp["reserveTokens"] = reserve
        prof["reserveTokens"] = reserve
    jput(sj, s)
    jput(os.path.join(AGENT, "harness-profile.json"), prof)
    return prov, new["models"][0]["id"], ctx


def install_extensions(engine, extra=(), verbose=True, show_ok=True, launch=True):
    """Run setup.sh without a terminal on stdin (the wizard already asked everything).
    verbose=False: full output to setup.log, one line per section on screen, warnings/failures in full.
    show_ok=False (start.sh): sections without problems leave no line at all.
    launch=False (wizard): setup.sh must not start halogen; the wizard starts the engine last itself."""
    args = (["--halogen", "auto"] if launch else []) if engine == "halogen" else ["--no-halogen"]
    cmd = ["bash", os.path.join(REPO, "setup.sh"), *args, *extra]
    env = dict(os.environ, HARNESS_STARTS_ENGINE="" if launch else "1", HARNESS_ENGINE=engine)  # HARNESS_ENGINE: setup.sh must not re-pick from the saved profile
    if verbose:
        return subprocess.run(cmd, stdin=subprocess.DEVNULL, env=env).returncode == 0
    os.makedirs(AGENT, exist_ok=True)
    p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, text=True, env=env)  # stderr stays on screen (download progress)
    sec, issues = None, []

    def close():
        if sec and not issues and not show_ok:
            print("\r\033[K", end="", flush=True)
        elif sec:
            mark = paint("31", "✗") if any("✗" in i for i in issues) else paint("33", "!") if issues else paint("32", "✓")
            print(f"\r\033[K  {mark} {sec}" + "".join(f"\n    {i.strip()}" for i in issues))

    with open(SETUP_LOG, "w") as log:
        for line in p.stdout:
            log.write(line)
            plain = re.sub(r"\033\[[0-9;]*m", "", line).rstrip()
            m = re.match(r"== (?:[0-9a-z-]+\. )?(.*) ==$", plain)
            if m:
                close()
                sec, issues = m.group(1), []
                print(f"  … {sec}", end="", flush=True)
            elif re.match(r"\s+[!✗] ", plain):
                issues.append(plain)
    close()
    return p.wait() == 0


# ---------- tune ----------
def hw_key(engine, model_id, weights=None, model=None):
    """weights/model: the key a result had under other answers (speeds() reads old results back); default the saved ones."""
    h = hardware()
    m = model or engine_model(engine)
    abl = engine in WEIGHTS_ENGINES and m in ABLITERATED and (weights or weights_pref()) == "abliterated"
    w = "|abliterated" if abl else ""  # stock keeps its old key
    if m != DEFAULT_MODEL:
        w = "|" + m + w  # another model is another speed
    elif engine == "gufo" and not w and saved_quant() != DEFAULT_QUANT:
        w = "|" + saved_quant()  # another quant is another speed; the default quant keeps its old key
    return hashlib.sha1(f"{engine}|{model_id}|{h['gpu']}|{h['ram_gb']}{w}".encode()).hexdigest()[:12]


def stream_once(port, model_id, prompt, max_tokens, timeout=None):
    """-> (prompt_tokens, ttft_s, decode_tps) for one streamed chat completion. Decode counts tokens, not chunks: an
    engine that drafts (osmos, halogen/gufo with a drafter) verifies several tokens per chunk, and counting chunks
    reported a third of the real rate."""
    t0, first, n, ptok, ctok = time.time(), None, 0, 0, 0
    body = {"model": model_id, "stream": True, "max_tokens": max_tokens, "temperature": 0,
            "stream_options": {"include_usage": True}, "messages": [{"role": "user", "content": prompt}]}
    for line in http(f"http://127.0.0.1:{port}/v1/chat/completions", body, timeout=timeout or TUNE_BUDGET_S):
        line = line.decode().strip()
        if not line.startswith("data:") or line.endswith("[DONE]"):
            continue
        d = json.loads(line[5:])
        if d.get("usage"):
            ptok = d["usage"].get("prompt_tokens", 0) or ptok
            ctok = d["usage"].get("completion_tokens", 0) or ctok
        for c in d.get("choices", []):
            delta = c.get("delta", {})
            if delta.get("content") or delta.get("reasoning_content"):
                first = first or time.time()
                n += 1
    end = time.time()
    return ptok, (first or end) - t0, ((ctok or n) / (end - first)) if first and end > first else 0.0


def tune(engine, model_id):
    """The quick subset of bench() (same prompts, same code path): 3 short code decodes and one 8K cold prefill, medians. Its
    decode_tps is short-code decode at near-zero context; its prefill_tps is at the 8K depth. Compare it only with bench's cells."""
    ctx = engine_ctx(engine)
    res = {"engine": engine, "model": model_id, "ctx": ctx, "when": time.strftime("%Y-%m-%d"), "protocol": BENCH_ID,
           **({"weights": weights_pref()} if engine in WEIGHTS_ASK and catalog_id(model_id) in ABLITERATED else {})}  # only these have an abliterated build
    try:
        b = bench(engine, model_id, runs=3, quick=True)
    except Exception as ex:
        res["error"] = str(ex)[:120]
        return res  # keep the shipped profile; a failed probe must never degrade defaults
    cells = b["median"]
    res["prefill_tps"] = round(next((v for k, v in cells.items() if k.startswith("prefill_")), 0))
    res["decode_tps"] = cells["decode_code"]
    res["samples"], res["spread"] = b["runs"], b["spread"]
    # derive settings from the window (not from the speed): answer room and compaction reserve
    if ctx:
        res["maxTokens"] = int(min(model_profile(model_id)["maxTokens"], max(1024, ctx // 8)))
        res["reserveTokens"] = reserve_for(ctx)
    cache = os.path.join(AGENT, "harness-tuned.json")
    c = jget(cache, {})
    c[hw_key(engine, model_id)] = res
    jput(cache, c)
    return res


# ---------- bench: fixed protocol, comparable across engines ----------
BENCH_ID = "bench-v1"  # bump when the prompts or the method change: results of different ids are not comparable
BENCH_WORDS = ("river lamp stone market garden window paper engine silver coast bridge harvest ladder copper forest signal "
               "morning thread orchard tunnel mirror harbor canvas furnace meadow compass lantern quarry violin pepper glacier").split()
BENCH_CODE = "Write a Python function that reverses a singly linked list, with a short docstring."
BENCH_PROSE = "Explain in plain words how a B-tree stays balanced as keys are inserted."


def bench_text(tokens, seed):
    """~tokens tokens of non-repeating filler (seeded: the same text on every engine, never served from a cache by accident)."""
    import random
    r = random.Random(seed)
    return " ".join(r.choice(BENCH_WORDS) + str(r.randrange(100)) for _ in range(int(tokens / 1.8)))


def power_profile():
    try:
        return open("/sys/firmware/acpi/platform_profile").read().strip()
    except OSError:
        return ""


def bench(engine, model_id, runs=3, depths=(2048, 8192, 32768), quick=False):
    """-> result dict. Warm-up (two 128-token code answers), then per run: a short code answer (256 tokens, greedy) at near-zero
    context, and unless quick a short prose answer, and for each depth a cold prefill (token counts from the server's usage) and a
    code answer after it (the prefix is cached, so that is decode at depth). quick (tune): one depth (8192, or 2048 on a window under
    16k) and one cold prefill, decode repeated. Medians over runs. Filler tokens are approximate: prefill divides by the server's count."""
    import statistics
    port, ctx = PROFILES["engines"][engine]["port"], engine_ctx(engine)
    depths = [d for d in ((8192, 2048) if quick else depths) if not ctx or d * 2 <= ctx][:1 if quick else None]
    s = lambda p, n: stream_once(port, model_id, p, n, 300 if quick else 900)  # long cold prompts on a slow engine
    for _ in range(2):
        s(f"[{uuid.uuid4().hex[:8]}] {BENCH_CODE}", 128)
    cells = {}
    add = lambda k, v: cells.setdefault(k, []).append(v)
    for i in range(runs):
        n = uuid.uuid4().hex[:8]
        add("decode_code", s(f"[{n}] {BENCH_CODE}", 256)[2])
        if not quick:
            add("decode_prose", s(f"[{n}] {BENCH_PROSE}", 256)[2])
        for d in depths:
            if quick and i:
                break  # quick: one cold prefill, the rest are decode repeats
            base = f"[{n}] Read this and answer the question after it.\n{bench_text(d, f'{n}{d}')}"
            ptok, ttft, _ = s(base, 1)
            add(f"prefill_{d}", ptok / ttft if ptok and ttft else 0)
            if not quick:
                add(f"decode_code_at_{d}", s(f"{base}\n\n{BENCH_CODE}", 256)[2])
    return {"protocol": BENCH_ID, "engine": engine, "model": model_id, "ctx": ctx, "runs": runs, "quick": quick, "power": power_profile(),
            "when": time.strftime("%Y-%m-%d %H:%M"), "median": {k: round(statistics.median(v), 1) for k, v in cells.items()},
            "spread": {k: round(max(v) - min(v), 1) for k, v in cells.items()}}


def cmd_bench(a):
    p = active() or sys.exit("no active profile: run ./harness setup")
    if p["engine"] == CUSTOM:
        sys.exit("./harness bench measures the engines this harness starts")
    if not served_models(PROFILES["engines"][p["engine"]]["port"], p["engine"]):
        sys.exit(f"{ename(p['engine'])} is not answering: start it first (./start.sh)")
    depths = tuple(int(x) for x in a.depths.split(","))
    print(f"== {BENCH_ID}: {engine_title(p['engine'], p['model'])}, {a.runs} runs, depths {list(depths)} (minutes; nothing else should be running) ==")
    r = bench(p["engine"], p["model"], a.runs, depths)
    for k, v in r["median"].items():
        print(f"  {k:22s} {v:8.1f} t/s   (spread {r['spread'][k]})")
    f = os.path.join(AGENT, "harness-bench.json")
    c = jget(f, {})
    c[f"{p['engine']}|{p['model']}|{r['when']}"] = r
    jput(f, c)
    print(f"  saved {f}; power profile: {r['power'] or 'unknown'} (watts are not read: note them yourself)")


# ---------- commands ----------
def active():
    return jget(os.path.join(AGENT, "harness-profile.json"))


def cmd_recommend(a):
    h, cap = hardware(), capacity()
    rec = recommend(h, cap)
    if a.json:
        print(json.dumps({"hardware": h, "capacity": cap, "recommendation": rec}, indent=1))
    else:
        print("\n".join(describe(rec, cap)))


def cmd_model_info(a):
    """One catalog field of a model (start-strata.sh and install-strata.sh read it); exit 1 when the model or field is not there."""
    v = catalog().get(a.id, {}).get(a.field)
    if v is None:
        sys.exit(1)
    print(" ".join(v) if isinstance(v, list) else v)


def cmd_ctx_info(a):
    print(gufo_ctx(a.name, capacity()))


def cmd_quant_info(a):
    gb = quant_gb(a.name)
    if gb is None:
        sys.exit(f"unknown quant {a.name}; known: {', '.join(q['name'] for q in PROFILES['recommend']['quants'])}")
    print(gb)


def cmd_detect(_):
    d = detect()
    d["gguf"] = ggufs()
    print(json.dumps(d, indent=1))


def catalog_id(served):
    """The catalog id a served model id is (qwen3.8-27b-q8 -> qwen38-27b-q8; Strata's qwen3.6-35b-a3b-ud-q4_k_xl ->
    qwen36-q4, from the catalog's own strata [family, size]): longest catalog id inside the served name, DEFAULT_MODEL
    when nothing matches."""
    norm = lambda s: re.sub(r"[^a-z0-9]", "", (s or "").lower())  # noqa: E731
    s = norm(served)
    hit = next((k for k, v in catalog().items()  # Strata serves its own ids: family and size both have to be in them
                if len(v.get("strata") or ()) == 2 and norm(v["strata"][0]) in s and norm(v["strata"][1]) in s), None)
    return hit or max((k for k in catalog() if norm(k) in s), key=len, default=DEFAULT_MODEL)


def speeds(engine, model=None):
    """{weights: newest ./harness tune result} for engine, stock and abliterated apart ("?" when an old result's key cannot
    tell). model: a served or catalog id; only results of that model."""
    out = {}
    for k, v in sorted(jget(os.path.join(AGENT, "harness-tuned.json"), {}).items(), key=lambda kv: kv[1].get("when", "")):
        if v.get("engine") != engine or not v.get("decode_tps") or (model and catalog_id(v.get("model")) != catalog_id(model)):
            continue
        w = v.get("weights")
        if catalog_id(v.get("model")) not in ABLITERATED:
            w = ""  # no abliterated build of the other models: older results labelled one by the saved answer
        elif not w:  # results before "weights" was saved: the weights are only in the key
            keys = {x: hw_key(engine, v.get("model"), x, catalog_id(v.get("model"))) for x in ("stock", "abliterated")}
            w = next((x for x, kx in keys.items() if kx == k), "?") if keys["stock"] != keys["abliterated"] else "?"
        out[w] = v
    if len(out) > 1:
        out.pop("?", None)  # an unlabelled old result beside labelled ones only confuses
    return out


def speed_text(engine, model=None):
    """'stock 34.3 t/s decode, 469 prefill (2026-11-15); abliterated ...', or '' when nothing was measured."""
    return "; ".join(f"{'' if w in ('?', '') else w + ' '}{v['decode_tps']} t/s decode, {v.get('prefill_tps', '?')} prefill "
                     f"({v.get('when', '?')}{'' if v.get('protocol') == BENCH_ID else ', old probe'})" for w, v in speeds(engine, model).items())


def last_speed(engine):
    t = [v for v in jget(os.path.join(AGENT, "harness-tuned.json"), {}).values() if v.get("engine") == engine and v.get("decode_tps")]
    t.sort(key=lambda v: v.get("when", ""))
    if not t:
        return "not measured"
    old = "" if t[-1].get("protocol") == BENCH_ID else " (old)"  # engine_menu explains (old)
    return f"{t[-1]['prefill_tps']}/{t[-1]['decode_tps']} t/s{old}"




def heap_holder(running):
    """Every engine we ship a launcher for that is up: only one stays loaded, so starting another stops these first."""
    return [e for e in running if PROFILES["engines"].get(e, {}).get("script")]


def engine_menu(running, esc=None):
    """One screen: state, model, last speed, what you lose, memory conflicts. -> engine name, or BACK."""
    cur = (active() or {}).get("engine")
    hw = hardware()  # fresh box: the recommended engine, halogen where it can run, then the NVIDIA engines on an NVIDIA box, then engines we can start for you
    rec = recommend(hw, capacity())["engine"]
    order = sorted(PROFILES["engines"], key=lambda e: (e != cur, e not in running, e != rec, not (hw["npu"] and e == "halogen"),
                                                       not (hw["gpu"] == "nvidia" and e == "strata"),
                                                       "script" not in PROFILES["engines"][e], e))
    holder = heap_holder(running)
    labels, loses, stops, speeds, serves = {}, {}, [], [], []
    # an engine without a launcher is only looked for: say where, it is yours to start
    states = {e: "running" if e in running else "not running" if PROFILES["engines"][e].get("script") else f"not found on :{PROFILES['engines'][e]['port']}"
              for e in order}
    vers = {e: installed_version(e) for e in order}
    w, ws, wv = max(len(ename(e)) for e in order), max(map(len, states.values())), max(map(len, vers.values()))
    for e in order:
        mods = running[e]["models"] if e in running else []
        state = f"{states[e]:{ws}s}"
        serves += [f"{ename(e)} serves {mods[0]}"] if mods else []  # a served id is up to ~30 columns: a note, not part of the row
        loses.setdefault(PROFILES["engines"][e].get("loses", "?"), []).append(ename(e))
        warn = bool(PROFILES["engines"][e].get("script") and holder and e not in running)
        stops += [e] * bool(warn)
        speeds.append(last_speed(e))
        labels[f"{'*' if e == cur else ' '}{'o' if e in running else '-'} {ename(e):{w}s}  {vers[e]:{wv}s}{'  ' if wv else ''}{state} | {speeds[-1]}{' !' if warn else ''}"] = e
    # what does not fit a row (gum cuts a long one off) is said once under the question
    every = [ename(e) for e in order]  # most engines lose the same: name the few that do not
    notes = serves + [f"recommended here: {ename(rec)}"] + [f"{'all but ' + ', '.join(x for x in every if x not in es) if len(every) > len(es) > len(every) / 2 else ', '.join(es)} "
                      f"{'loses' if len(es) == 1 else 'lose'}: {v}" for v, es in loses.items()]
    if any(s != "not measured" for s in speeds):
        notes.append("t/s = prefill/decode on code, from ./harness tune" + ("; (old) = an older probe, re-run it" if any(s.endswith("(old)") for s in speeds) else ""))
    if stops:
        notes.append(f"! = stops {', '.join(map(ename, holder))} first: only one engine stays loaded")
    choice = choose("Which engine? (* = current, o = running)", list(labels), False, esc, notes)
    return BACK if choice is BACK else labels[choice]


def stop_process(port):
    """SIGTERM the process listening on port (ours only: ss shows pids of the current user), wait until it lets go."""
    pids = set(re.findall(r"pid=(\d+)", subprocess.run(["ss", "-ltnpH", f"sport = :{port}"], capture_output=True, text=True).stdout))
    if not pids:
        return False
    for p in pids:
        try:
            os.kill(int(p), 15)
        except OSError:
            return False
    for _ in range(30):
        if not subprocess.run(["ss", "-ltnH", f"sport = :{port}"], capture_output=True, text=True).stdout.strip():
            return True
        time.sleep(2)
    return False


def engine_containers(engine):
    """Ids of the containers serving engine. halogen and gufo run in containers whose image name says so, so an unrelated
    container on the same port is left alone; any other engine is matched by port alone."""
    port = PROFILES["engines"][engine]["port"]
    ids = tool("docker", "ps", "-q", "--filter", f"publish={port}").stdout.split()
    if engine not in ("halogen", "gufo"):
        return ids
    return [i for i in ids if engine in tool("docker", "inspect", "-f", "{{.Config.Image}}", i).stdout.lower()]


def stop_engine(engine, running):
    port = PROFILES["engines"][engine]["port"]
    cid = engine_containers(engine)
    if cid:
        tool("docker", "stop", "-t", "30", *cid)
    elif not stop_process(port):  # Strata runs as a plain process, not a container
        print(f"  could not find what serves {ename(engine)} on :{port}; stop it yourself")
        return False
    # the ling-tiny sidecar is a GPU llama-server holding ~8 GiB too (measured: GTT 9.7 -> 1.5 GiB when stopped);
    # it restarts on demand, so stopping it here frees the memory for the next engine
    tool("systemctl", "--user", "stop", "ling-tiny.service")
    for _ in range(10):
        if gtt_gb() < 3:
            break
        time.sleep(2)
    print(f"  {mem_after_stop()}")
    return True


def free_gb(path):
    """GB free where path is, or would be created: the nearest existing parent."""
    while not os.path.isdir(path) and path != os.path.dirname(path):
        path = os.path.dirname(path)
    return shutil.disk_usage(path).free // 2**30


def halogen_ckpt():
    """The stock checkpoint: its folder also holds what every halogen checkpoint needs (tokenizer, n-gram, npu/)."""
    return subprocess.run(["bash", os.path.join(REPO, "server", "start-halogen.sh"), "--print-stock"], capture_output=True, text=True).stdout.strip()


# ---------- weights (stock / abliterated), one saved answer the halogen and gufo launchers read ----------
WEIGHTS = os.path.join(AGENT, "harness-weights")
WEIGHTS_ENGINES = {"halogen": ("--print-ckpt", 89), "gufo": ("--print-model", 112), "osmos": ("--print-weights", 1),
                   "llamacpp": ("--print-lora", 1)}  # launcher flag, download GB (llamacpp: a 2.3 MB LoRA over the stock GGUF)
WEIGHTS_ASK = (*WEIGHTS_ENGINES, "strata")  # strata: the answer picks which model scripts/install-strata.sh installs and starts
WEIGHTS_LABELS = {"stock": "stock Qwen3.8-Flash-Next (default)",
                  "abliterated": "abliterated (refusals removed; docs/ENGINES.md)"}


def weights_pref():
    try:
        return open(WEIGHTS).read().strip() or "stock"
    except OSError:
        return "stock"


def weights_file(engine):
    """The main weights file the engine's launcher will load with the saved answer (it may not exist yet)."""
    c = PROFILES["engines"][engine]
    return subprocess.run(["bash", os.path.join(REPO, c["script"]), WEIGHTS_ENGINES[engine][0]], capture_output=True, text=True).stdout.strip()


def weights_download_gb(engine):
    """GB scripts/download-abliterated.sh still has to fetch: 0 when complete; gufo also needs the stock MTP head."""
    if engine == "osmos":  # libosmos's installer (run by start-osmos.sh) reuses its own cache or fetches the 0.6 GB patch itself
        return 0
    f = weights_file(engine)
    if not os.path.isfile(f):
        return WEIGHTS_ENGINES[engine][1]
    mtp = os.path.join(os.path.dirname(os.path.dirname(f)), "mtp-Qwen3.8-Flash-Next-shared-Q8_0.gguf")
    return 5 if engine == "gufo" and not os.path.isfile(mtp) else 0


def weights_labels(engine):
    """The stock/abliterated labels to offer: a profile's own (osmos runs DeepSeek, not Qwen), else the Qwen ones."""
    return PROFILES["engines"].get(engine, {}).get("weights_labels") or WEIGHTS_LABELS


def ask_weights(engine, back=False):
    """Which weights the engine loads by default; current answer first. -> True if the answer changed, or BACK."""
    if engine not in WEIGHTS_ASK or not sys.stdin.isatty() or engine == "osmos" and osmos_inkling():
        return False
    m = engine_model(engine)
    own = engine in ("gufo", "strata", "llamacpp") and m not in ABLITERATED  # a model without an abliterated build (Qwen3.6, Strata's packs)
    cur = "stock" if own else weights_pref()
    labels = {"stock": f"stock {short_model(m)} (its only build)",
              "abliterated": f"abliterated: switch to {short_model(abl_model(m))}, which has one"} if own else (
        {"stock": f"stock {quant_of(m)} (Unsloth)", "abliterated": f"abliterated {quant_of(m)} (huihui-ai; refusals removed)"}
        if m != DEFAULT_MODEL else weights_labels(engine))
    opts = sorted(labels, key=lambda k: k != cur)
    measured = speeds(engine, engine_model(engine) if engine in ("gufo", "strata") else None)
    notes = [f"measured here: {w + ' ' if w else ''}{v['decode_tps']} t/s decode, {v.get('prefill_tps', '?')} prefill ({v.get('when', '?')})"
             for w, v in measured.items() if w != "?"]
    choice = choose(f"Which weights should {ename(engine)} load? one answer for every engine, used on its next start"
                    + ("; abliterated on Strata = gufo's orca-uncensored shards" if engine == "strata" else ""),
                    [labels[k] for k in opts], False, BACK if back else None, notes)
    if choice is BACK:
        return BACK
    pick = {labels[k]: k for k in opts}[choice]
    if own and pick == "stock":
        return False  # nothing to change, and the saved answer still holds for the Flash-Next engines
    os.makedirs(AGENT, exist_ok=True)
    open(WEIGHTS, "w").write(pick + "\n")
    return pick != cur


def stock_ready(quant=DEFAULT_QUANT):
    """Every shard of the stock quant is on disk (scripts/download-stock.sh puts them there; Strata and an unpicked
    gufo use UD-Q4_K_XL). Shard 1 is tiny and lands first, so it alone proves nothing."""
    first = subprocess.run(["bash", os.path.join(REPO, "server", "start-gufo-flashnext.sh"), "--print-model"], capture_output=True, text=True,
                           env=dict(os.environ, GUFO_WEIGHTS="stock", GUFO_QUANT=quant)).stdout.strip()
    m = re.search(r"-00001-of-(\d{5})\.gguf$", first)
    return bool(m) and all(os.path.isfile(first.replace("-00001-of-", f"-{i:05d}-of-")) for i in range(1, int(m.group(1)) + 1))


G27_DRAFT = "Qwen3.8-27B-DFlash2-Q4_K_M.gguf"  # same name in server/start-gufo-flashnext.sh and scripts/download-qwen38-27b.sh


def is_27b(m):
    return m in ("qwen38-27b", "qwen38-27b-q8")


def g27_ready(model="qwen38-27b"):
    """The 27B target (Q4 or Q8), its DFlash2 draft and the vision projector are all in the folder the gufo launcher reads."""
    first = subprocess.run(["bash", os.path.join(REPO, "server", "start-gufo-flashnext.sh"), "--print-model"], capture_output=True, text=True,
                           env=dict(os.environ, GUFO_FAMILY=model)).stdout.strip()
    d = os.path.dirname(first)
    return bool(first) and all(os.path.isfile(os.path.join(d, f)) for f in (os.path.basename(first), G27_DRAFT, "mmproj-BF16.gguf"))


def strata_installed():
    """Strata's setup already installed the saved model (--print-run: its run-<model>.sh exists; Flash-Next also needs the saved weights answer)."""
    return bool(subprocess.run(["bash", os.path.join(REPO, "server", "start-strata.sh"), "--print-run"], capture_output=True, text=True).stdout.strip())


def strata_how():
    """How scripts/install-strata.sh installs Strata here: its Docker image by default, a source build otherwise."""
    img = subprocess.run(["bash", os.path.join(REPO, "server", "start-strata.sh"), "--print-image"], capture_output=True, text=True).stdout.strip()
    return f"pulls {img} (~1.5 GB, no compile)" if img else "git clone into ~/coding/Strata if needed, then its setup builds the engine"


def halogen_preflight():
    """What halogen's launcher needs on the host, checked before the 124 GB download -> list of problems (empty = fine)."""
    if not shutil.which("docker"):
        return ["docker is not installed (setup will not sudo-install it)"]
    out = []
    try:
        if subprocess.run(["docker", "info"], capture_output=True, timeout=20).returncode:
            out.append("docker is installed but this user cannot use it (start the service, or add yourself to the docker group)")
    except (OSError, subprocess.TimeoutExpired):
        out.append("docker did not answer")
    out += [f"{d} is missing (the NPU/GPU driver is not loaded)" for d in ("/dev/accel/accel0", "/dev/kfd") if not os.path.exists(d)]
    out += [f"/usr/lib/{f} is missing (install the XRT runtime; the launcher mounts it)" for f in ("libxrt_coreutil.so.2", "libxrt_core.so.2", "libxrt_driver_xdna.so.2")
            if not os.path.exists(f"/usr/lib/{f}")]
    if not out:  # the plugin is built against one xrt: after an xrt-only update it cannot load, and the NPU engine dies with "No such device"
        try:
            r = subprocess.run(["ldd", "-r", "/usr/lib/libxrt_driver_xdna.so.2"], capture_output=True, text=True, timeout=20)
            bad = re.findall(r"undefined symbol: (\S+)", r.stdout + r.stderr)
        except (OSError, subprocess.TimeoutExpired):
            bad = []  # no ldd: the launcher's own check still runs
        if bad:
            out.append(f"xrt-plugin-amdxdna does not match the installed xrt (it needs {bad[0]}): update/rebuild both together, then re-run - halogen's NPU engine cannot start until then")
    return out


def plan_start(engine, running, ask_start=True):
    """Ask everything starting engine needs (stop the memory holder, download, start); nothing runs yet.
    ask_start=False (start.sh): starting the saved engine needs no question, only stopping another one does.
    -> plan dict, or None when there is no launcher or the user declined."""
    c = PROFILES["engines"][engine]
    if not c.get("script"):
        print(f"{ename(engine)} is not running on :{c['port']}: {c.get('start', 'start it yourself, then pick it again')}")
        return None
    plan = {"stop": [], "download": False, "weights": None}
    if engine == "osmos":  # its expert cache is sized from what is free at start: ~6 GiB less measured 34 -> 24 t/s
        print("Tip: close browsers and other big apps before osmos starts. It caches\n"
              "  experts in whatever memory is free, and every GiB it misses is read from the SSD, slowing decode.")
    if engine == "halogen":
        problems = halogen_preflight()
        if problems:
            print(f"halogen cannot start on this box yet (nothing was downloaded):" + "".join(f"\n  \u2717 {p}" for p in problems))
            return None
    holder = [e for e in heap_holder(running) or [] if e != engine]
    if holder:
        if not ask_yn(f"Stop {', '.join(map(ename, holder))}? only one engine stays loaded, {ename(engine)} needs the GPU memory; the ling-tiny sidecar stops too"):
            return None
        plan["stop"] = holder
    if engine == "halogen" and not os.path.isfile(halogen_ckpt()):
        base = os.environ.get("HALOGEN_MODELS") or os.path.expanduser("~/models")  # scripts/download-halogen.sh's folder
        free, need = free_gb(base), PROFILES["recommend"]["halogen_download_gb"] + 11  # the script's margin: 135 for 124
        if free < need:  # before the question: nobody should agree to a download that cannot finish
            print(f"halogen needs ~{need} GB for its download and only {free} GB are free where {base} is.\n"
                  f"  Free some space, or put it on a bigger disk: HALOGEN_MODELS=/path/to/models ./setup.sh (the folder is remembered).")
            return None
        if not ask_yn(f"Download the halogen checkpoint? not on this box yet: ~124 GB into {base}, {free} GB free, "
                      "resumable, checked against the repo afterwards"):
            return None
        plan["download"] = True
    m = engine_model(engine)
    own = m != DEFAULT_MODEL  # another model than Flash-Next: the stock/abliterated answer and its downloads do not apply
    if engine == "strata" and not strata_installed() and own:
        free = shutil.disk_usage(os.path.expanduser("~")).free // 2**30
        e = catalog()[m]
        if not ask_yn(f"Install Strata for {model_name(m)}? not installed yet. {strata_how()}; "
                      f"Strata downloads ~{e['download_gb']} GB, {free} GB free, resumable"):
            return None
        plan["install"] = "strata"
    elif engine == "strata" and not strata_installed():
        free = shutil.disk_usage(os.path.expanduser("~")).free // 2**30
        what = "uses gufo's orca-uncensored shards in place" if weights_pref() == "abliterated" else "uses the stock UD-Q4_K_XL that gufo shares in place"
        if not ask_yn(f"Install Strata for the {weights_pref()} weights? not installed yet. {strata_how()}; it {what}; "
                      "switching answers rebuilds its small pack, no weights download"):
            return None
        plan["install"] = "strata"
        # abliterated Strata runs gufo's orca-uncensored shards in place: say so now, not when the install fails
        if weights_pref() == "abliterated" and not os.path.isfile(weights_file("gufo")):
            if not ask_yn(f"Download gufo's abliterated GGUF? the abliterated Strata model; ~{WEIGHTS_ENGINES['gufo'][1]} GB, "
                          f"gated: accept the terms on its Hugging Face page and run `hf auth login` first; "
                          f"{free} GB free, resumable"):
                return None
            plan["weights"], plan["weights_gb"] = "gufo", WEIGHTS_ENGINES["gufo"][1]
    # gufo and Strata share one stock GGUF (Unsloth's UD-Q4_K_XL, latest Dynamic): fetch it once, whichever comes first.
    # gufo may follow the hardware pick to a smaller quant; Strata only runs UD-Q4_K_XL.
    q = saved_quant(engine)
    # llama.cpp needs the stock GGUF for both answers: abliterated is a LoRA on top of it
    if (engine in ("gufo", "strata") and weights_pref() == "stock" or engine == "llamacpp") and not own and not stock_ready(q):
        free = shutil.disk_usage(os.path.expanduser("~")).free // 2**30
        if not ask_yn(f"Download the stock weights? Unsloth {q}, "
                      f"{'shared by gufo and Strata' if q == DEFAULT_QUANT else 'picked for this hardware'}; "
                      f"~{quant_gb(q)} GB, {free} GB free, resumable"):
            return None
        plan["stock"] = q
    if engine == "gufo" and is_27b(m) and not g27_ready(m):
        free = shutil.disk_usage(os.path.expanduser("~")).free // 2**30
        # Q4 and Q8 share the draft and projector: the script counts only the missing files
        r = subprocess.run(["bash", os.path.join(REPO, "scripts", "download-qwen38-27b.sh"), "--print-gb"], capture_output=True, text=True,
                           env=dict(os.environ, GUFO_FAMILY=m)).stdout.strip()
        gb = int(r) if r.isdigit() else catalog()[m]["download_gb"]
        if not ask_yn(f"Download Qwen3.8 27B {'UD-Q8_K_XL' if m.endswith('-q8') else 'UD-Q4_K_XL'}{' abliterated (huihui-ai)' if m in ABLITERATED and weights_pref() == 'abliterated' else ''}? "
                      f"~{gb} GB of {catalog()[m]['download_gb']} still to fetch (target, DFlash2 draft, vision projector), {free} GB free, resumable"):
            return None
        plan["g27"], plan["g27_gb"] = m, gb
    if engine == "osmos" and osmos_inkling() and subprocess.run(["bash", os.path.join(REPO, c["script"]), "--check"], capture_output=True).returncode:
        free = shutil.disk_usage(os.path.expanduser("~")).free // 2**30
        if not ask_yn(f"Download osmos's Inkling 2 Mini pack? the release's weights + its drafter, ~{catalog()[m]['download_gb']} GB, {free} GB free, "
                      "fetched and checked by libosmos's installer"):
            return None
        plan["osmos_pack"] = True
    gb = weights_download_gb(engine) if engine in WEIGHTS_ENGINES and not own and weights_pref() != "stock" else 0
    if gb:
        free = shutil.disk_usage(os.path.expanduser("~")).free // 2**30
        if not ask_yn(f"Download the {weights_pref()} {ename(engine)} weights? not on this box yet{' (or their MTP head)' * (engine == 'gufo')}; "
                      f"~{gb} GB, {free} GB free, resumable"):
            return None
        plan["weights"], plan["weights_gb"] = engine, gb
    if engine == "osmos" and not osmos_inkling() and weights_pref() == "abliterated" and weights_file(engine) == "stock":
        print("  osmos switches to the abliterated patch on start: libosmos reuses it if on disk, else fetches it (0.6 GB)")
    if ask_start and not ask_yn(f"Start {ename(engine)}? runs {c['script']} in the background, log {engine_log(engine)}", True):
        return None
    return plan


def prepare_start(plan, running):
    """The parts of a start plan an upgrade also needs first: stop the memory holder, download. Runs once (clears them)."""
    stop, dl, wd = plan["stop"], plan["download"], plan.get("weights")
    plan["stop"], plan["download"], plan["weights"] = [], False, None
    if any(not stop_engine(e, running) for e in stop or []):
        return False
    if dl and subprocess.run(["bash", os.path.join(REPO, "scripts", "download-halogen.sh"), "--yes"]).returncode:
        return False
    if wd and subprocess.run(["bash", os.path.join(REPO, "scripts", "download-abliterated.sh"), wd, "--yes"]).returncode:
        return False  # before the install: abliterated Strata installs on top of gufo's shards
    g27 = plan.pop("g27", False)
    plan.pop("g27_gb", None)
    if g27 and subprocess.run(["bash", os.path.join(REPO, "scripts", "download-qwen38-27b.sh"), "--yes"], env=dict(os.environ, GUFO_FAMILY=g27)).returncode:
        return False
    if plan.pop("osmos_pack", False) and subprocess.run(["bash", os.path.join(REPO, PROFILES["engines"]["osmos"]["script"]), "--install"]).returncode:
        return False
    sq = plan.pop("stock", False)
    if sq and subprocess.run(["bash", os.path.join(REPO, "scripts", "download-stock.sh"), "--yes"], env=dict(os.environ, GUFO_QUANT=sq)).returncode:
        return False  # same for stock: Strata installs on top of the shared UD-Q4_K_XL
    inst = plan.pop("install", None)
    return not (inst and subprocess.run(["bash", os.path.join(REPO, "scripts", f"install-{inst}.sh"), "--yes"]).returncode)


def engine_log(engine):
    os.makedirs(LOGDIR, exist_ok=True)
    return os.path.join(LOGDIR, f"{engine}.log")


def start_engine(engine, running, plan=None, wait_s=900):
    """Start an engine we ship a launcher for (halogen, gufo, osmos); asks first unless given a plan. -> True once it answers."""
    plan = plan or plan_start(engine, running)
    if plan is None or not prepare_start(plan, running):
        return False
    c = PROFILES["engines"][engine]
    log = open(engine_log(engine), "w")
    env = dict(os.environ)
    pick = jget(PICK, {}) or {}
    if engine == "gufo" and pick.get("engine") == "gufo" and pick.get("ctx"):
        env.setdefault("GUFO_CTX", str(pick["ctx"]))  # the accepted hardware pick; an explicit GUFO_CTX still wins
    proc = subprocess.Popen(["bash", os.path.join(REPO, c["script"])], stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True, env=env)
    t0 = time.time()
    ready = (PROFILES["engines"][engine].get("ready") or "").strip()  # e.g. osmos lists its model while still loading
    try:
        while time.time() - t0 < wait_s:
            if served_models(c["port"], engine) and not (ready and not answers_ready(c["port"], ready)):
                print(f"\r\033[K  {ename(engine)} is up after {int(time.time() - t0)}s")  # replaces the waiting line and its log excerpt
                return True
            if proc.poll() is not None:  # launchers exec the server: an exited launcher means the start failed
                whole = open(engine_log(engine)).read()
                tail = whole.strip().splitlines()[-3:]
                print(f"\n  {ename(engine)} failed to start:" + "".join(f"\n    {l}" for l in tail))
                if engine == "strata":
                    print("".join(f"\n  -> {x}" for x in strata_doctor.explain(whole)), end="")
                return False
            print("\r\033[K" + wait_line(engine, c["port"], int(time.time() - t0), proc.pid), end="", flush=True)
            time.sleep(3)
    except KeyboardInterrupt:
        print(f"\n  {ename(engine)} keeps loading in the background (pid {proc.pid}); log: {engine_log(engine)}"
              f"\n  up once this answers: curl -s localhost:{c['port']}/v1/models"
              f"\n  then re-run ./setup.sh for what needs it (model pick, speed probe); it reuses the running {ename(engine)}")
        sys.exit(130)  # what is left needs the engine; not_answering's "start it" would start a second one
    print(f"\n  {ename(engine)} did not answer in {wait_s}s; it may still be loading (pid {proc.pid}), see {engine_log(engine)}")
    return False


def wait_line(engine, port, secs, pid):
    """One status line cut to the terminal width (a wrapped line breaks the \\r rewrite): elapsed, disk read so far
    (a load or a checksum is mostly reads), the log's last line."""
    try:
        gb = int(next(l for l in open(f"/proc/{pid}/io") if l.startswith("read_bytes")).split()[1]) / 2**30
    except (OSError, StopIteration, ValueError):
        gb = 0  # not Linux, or the process is gone
    try:
        last = next((l.strip() for l in reversed(open(engine_log(engine), errors="replace").read().splitlines()) if l.strip()), "")
    except OSError:
        last = ""
    s = f"  waiting for {ename(engine)} on :{port} {secs}s" + (f", read {gb:.0f} GB" if gb >= 1 else "") + " (Ctrl-C keeps it loading)"
    return (s + (f" | {last}" if last else ""))[:shutil.get_terminal_size((100, 20)).columns - 1]


def gufo_memory(served):
    """'82.8 of 124.0 GiB GPU memory in use: model files 33.4, prompt cache 20.5, 2 sessions x 262144 context', from gufo's
    last load in its log ('' when the log has none). gufo reserves every session's context at load, so a big number at
    idle is expected, not a second model."""
    try:
        lines = open(engine_log("gufo"), errors="replace").read().splitlines()
    except OSError:
        return ""
    kv = lambda line: dict(p.split("=", 1) for p in line.split() if "=" in p)  # noqa: E731
    done = next((kv(l) for l in reversed(lines) if "event=load_completed" in l), None)
    if not done or "gpu_device_used_mib" not in done or done.get("model") != served:  # a log of another load says nothing
        return ""
    cache = next((kv(l) for l in reversed(lines) if "[cache]" in l and "event=configured" in l), {})
    used, total = int(done["gpu_device_used_mib"]) / 1024, int(done["gpu_device_total_mib"]) / 1024
    try:  # the files the launcher loads (target, draft, projector), next to each other on disk
        first = subprocess.run(["bash", os.path.join(REPO, PROFILES["engines"]["gufo"]["script"]), "--print-model"],
                               capture_output=True, text=True, env=dict(os.environ, GUFO_FAMILY=catalog_id(served))).stdout.strip()
        files = sum(os.path.getsize(os.path.join(os.path.dirname(first), f)) for f in os.listdir(os.path.dirname(first))
                    if f.endswith(".gguf") and (f.startswith(os.path.basename(first).split("-00001-of-")[0]) or "DFlash" in f or "mtp-" in f or f.startswith("mmproj"))) / 2**30
    except (OSError, KeyError):
        files = 0
    parts = ([f"model files {files:.1f}"] if files else []) + (
        [f"prompt cache {int(cache['capacity_bytes']) / 2**30:.1f}"] if cache.get("capacity_bytes") else []) + (
        [f"{done['sessions']} sessions x {done.get('context_tokens', '?')} context"] if done.get("sessions") else [])
    return f"{used:.1f} of {total:.1f} GiB GPU memory in use" + (f": {', '.join(parts)}" if parts else "")


def choose_engine(running, esc):
    """The model menu (Strix Halo) or the engine menu -> (engine, the route's model or None), or BACK.
    Esc in the engine menu reached from the model menu goes back to the model menu."""
    while True:
        route = ask_route(esc)
        if route is BACK:
            return BACK
        if route and route is not ENGINE_MENU:
            return route
        eng = engine_menu(running, BACK if route else esc)
        if eng is not BACK:
            return eng, None
        if not route:  # the engine menu was the first menu
            return BACK


def not_started(eng, esc):
    """plan_start said no for a named engine (a question declined, or the box cannot run it; it printed why): back, else leave."""
    if esc is BACK:
        print(f"  {ename(eng)} not started; back to the menu")
        return BACK
    sys.exit(f"{ename(eng)} not started. Run ./setup.sh or ./harness config to pick again.")


def not_answering(eng):
    e = PROFILES["engines"][eng]
    return f"{ename(eng)} is not answering on :{e['port']} - {e.get('start', 'start it first')}"


def pick_engine(d, yes, want=None, on_pick=None, esc=None):
    """Questions only: -> (engine, start plan or None if it is already up). Nothing starts here.
    on_pick(engine) runs right after the choice, before any start/stop/download question (the update check).
    With esc=BACK every menu inside returns (BACK, None) instead of quitting, so ./harness config can go back."""
    running = {x["engine"]: x for x in d["engines"]}
    if want:
        eng = want.partition("/")[0]
        if eng not in PROFILES["engines"]:
            sys.exit(f"unknown engine {eng}; known: {', '.join(PROFILES['engines'])}")
        c = PROFILES["engines"][eng]
        if serves_other(eng, running):  # ./harness use gufo after a model change in ./harness config
            if not (sys.stdin.isatty() and ask_yn(f"Restart {ename(eng)}? it serves {running[eng]['models'][0]}, your saved model is {engine_model(eng)}", True)):
                print(f"  ! {ename(eng)} keeps serving {running[eng]['models'][0]} until it restarts on {engine_model(eng)}")
                return eng, None
            plan = plan_start(eng, {k: v for k, v in running.items() if k != eng}, ask_start=False) or not_started(eng, esc)
            if plan is BACK:
                return BACK, None
            plan["stop"].append(eng)
            return eng, plan
        if c.get("script") and not any(x["port"] == c["port"] for x in d["engines"]):
            # named but down: ask what the wizard asks (stop the memory holder, download) and start it, instead of telling the user to do it by hand
            plan = plan_start(eng, running) or not_started(eng, esc)
            return (BACK, None) if plan is BACK else (eng, plan)
        return eng, None
    if yes or not sys.stdin.isatty():  # unattended: only what is already up, halogen first
        if not running:
            sys.exit("no engine answering on the usual ports. Start one (see docs/ENGINES.md, or `./setup.sh --halogen auto`), then re-run.")
        return next(iter(sorted(running, key=lambda e: e != "halogen"))), None
    if not running:
        yours = [ename(e) for e, c in PROFILES["engines"].items() if not c.get("script")]
        print("\n".join(textwrap.wrap("No engine is running yet. The one you pick is downloaded, installed and started for you, "
                                      f"except {', '.join(yours)}: start those yourself.", term_width())))
    before_pref = model_pref()
    while True:  # Esc in any menu below, or a start that did not happen, comes back to the first menu
        pick = choose_engine(running, esc)
        if pick is BACK:
            return BACK, None
        eng, route_model = pick
        # the model answer before this run: ask_route has already saved the new one
        before = before_pref if eng in catalog().get(before_pref, {}).get("engines", ()) else DEFAULT_MODEL
        if not (route_model and route_model != DEFAULT_MODEL) and not offer_recommendation(eng):  # a model picked by name is already answered
            if ask_model(eng, back=True) is BACK:
                continue
        # weights stock/abliterated only mean something for a model with an abliterated build
        weights_changed = ask_weights(eng, back=True) if engine_model(eng) in ABLITERATED else False
        if weights_changed is BACK:
            continue
        upgrading = on_pick(eng) if on_pick else False
        # the server keeps what it loaded at start: a new default needs a restart (an upgrade restarts it anyway)
        what = "weights" if weights_changed else "model"
        restart = (weights_changed or engine_model(eng) != before or serves_other(eng, running)) and eng in running and not upgrading and ask_yn(
            f"Restart {ename(eng)}? it is running different {what} than your new default", True)
        if eng in running and not restart:
            return eng, None
        if upgrading:  # every upgrader starts the engine itself: "Start?" would be a question with only one answer
            print(f"  the {ename(eng)} upgrade starts it")
        plan = plan_start(eng, {k: v for k, v in running.items() if k != eng}, ask_start=not (upgrading or restart))
        if plan:
            break
        print(f"  {ename(eng)} not started; pick again, or Esc at the first menu to {'go back' if esc is BACK else 'quit'}")
    if restart:
        plan["stop"].append(eng)  # prepare_start stops it, start_engine brings it back on the new weights
    return eng, plan


def bring_up(d, eng, plan, yes, mod="", esc=None):
    """Run the start plan (if any), then pick the model. -> (engine, model), or (BACK, None)."""
    if plan:
        if not start_engine(eng, {x["engine"]: x for x in d["engines"]}, plan):
            sys.exit(not_answering(eng))
        d["engines"] = detect()["engines"]
    running = {x["engine"]: x for x in d["engines"]}
    port = PROFILES["engines"][eng]["port"]
    if eng not in running:  # strata / llama.cpp / gufo share :8080: whatever answers there is what you started
        same = [x for x in d["engines"] if x["port"] == port]
        if not same:
            sys.exit(not_answering(eng))
        running[eng] = same[0]  # we started (or you named) eng: keep it, whatever detect calls that port
    models = running[eng]["models"]
    if not models:
        sys.exit(f"{ename(eng)} lists no models")
    if mod and mod not in models:  # a typo'd `use gufo/<model>` used to be written as the model id: pi got 404s until the next use
        sys.exit(f"{ename(eng)} serves {', '.join(models)}, not {mod}")
    # Esc here (the wizard's last question, after install and start) takes the first model: cancel() left the profile on the old engine
    m = mod or choose("Which model?", models, yes, esc if esc is not None else models[0])
    return (BACK, None) if m is BACK else (eng, m)


def pick_target(d, yes, want=None, esc=None):
    eng, plan = pick_engine(d, yes, want, esc=esc)
    if eng is BACK:
        return BACK, None
    return bring_up(d, eng, plan, yes, (want or "").partition("/")[2], esc)


# ---------- extras (asked once, answers saved) ----------
EXTRAS = os.path.join(AGENT, "harness-extras.json")
# file extension -> serena language_servers name
LANGS = {".py": "python", ".ts": "typescript", ".tsx": "typescript", ".js": "typescript", ".jsx": "typescript",
         ".go": "go", ".rs": "rust", ".java": "java", ".kt": "kotlin", ".rb": "ruby", ".cs": "csharp",
         ".php": "php", ".swift": "swift", ".c": "cpp", ".cc": "cpp", ".cpp": "cpp", ".h": "cpp"}


IN_MENU = False  # ./harness config: Esc at a yes/no answers No (back to the menu) instead of quitting it


def ask_yn(question, default=False):
    """Every yes/no in the wizard: gum choose Yes/No when installed (same look as choose()), else a plain prompt."""
    if not sys.stdin.isatty():
        no_terminal(question, "yes" if default else "no")
        return default
    if shutil.which("gum"):
        # gum choose, not gum confirm: confirm exits 1 for both "No" and Esc, so Esc could not cancel
        print(question_text(question))  # our headline and detail: gum cuts a --header off, and would put it under them
        print(gum_hint("no" if IN_MENU else None))
        # stdout only: gum draws its menu on stderr, so capturing stderr leaves a blank prompt
        r = subprocess.run(["gum", "choose", "--no-show-help", "--header", "", "--selected", "Yes" if default else "No", "Yes", "No"],
                           stdout=subprocess.PIPE, text=True)
        out = r.stdout.strip()
        if out in ("Yes", "No"):
            print(out.lower())
            return out == "Yes"
        if r.returncode == 1 and IN_MENU:  # Esc in ./harness config: No, back to the menu like its submenus
            print("no")
            return False
        if r.returncode in (1, 130):  # Esc / Ctrl-C cancel the wizard: a yes/no is not a menu, there is no level above
            cancel()
    print(question_text(question))
    esc = "Esc = no" if IN_MENU else "Esc cancels"
    prompt = ("[Y/n]" if default else "[y/N]") + " " + paint("2", f"(Enter = {'yes' if default else 'no'}, {esc})") + " "
    print(prompt, end="", flush=True)
    while True:
        k = key().lower()
        if k == "\x1b" and IN_MENU:
            print("no")
            return False
        if k == "\x1b":
            cancel()
        if k in ("\r", "\n", "y", "n"):
            a = default if k in ("\r", "\n") else k == "y"
            print("yes" if a else "no")
            return a
        print("\n" + paint("2", f"  y or n, Enter for the default, {esc}") + "\n" + prompt, end="", flush=True)


def sidecar_pref():
    """'1' / '0' / '' (never asked) from the file setup.sh reads."""
    try:
        return open(os.path.join(AGENT, "harness-sidecar")).read().strip()
    except OSError:
        return ""


SIDECAR_MIN_GB = 24  # the sidecar holds ~8 GiB of GPU memory: below this it competes with the main model for the card


def sidecar_default():
    """Default answer for the sidecar: on where the GPU has room (Strix Halo, big cards, or a failed probe), off on small cards."""
    fast = capacity()["fast_gb"]
    return not 0 < fast < SIDECAR_MIN_GB


def set_sidecar(on):
    os.makedirs(AGENT, exist_ok=True)
    open(os.path.join(AGENT, "harness-sidecar"), "w").write("1\n" if on else "0\n")


def ask_sidecar():
    """Ask on every interactive wizard run whether to run the ling-tiny sidecar, pre-filled with the saved answer (else the
    GPU-size default); setup.sh reads the saved answer. Without a terminal the saved answer stands."""
    if not sys.stdin.isatty():
        return
    dl = "" if glob.glob(os.path.expanduser("~/.models/ling3.0-tiny/*.gguf")) + glob.glob(os.path.join(REPO, "models/ling3.0-tiny/*.gguf")) else ", first run downloads 4.5 GB"
    saved = sidecar_pref()
    default, fast = (saved == "1") if saved else sidecar_default(), capacity()["fast_gb"]
    room = f" Your GPU has {fast:g} GB, so the default is No." if not default and not saved else ""
    set_sidecar(ask_yn(f"Run the ling-tiny sidecar? ~2x faster commits/compaction, holds ~8 GiB GPU memory{dl}.{room} "
                       "(No = the main model writes them)", default))


def no_pi():
    """True when the user opted out of pi wiring (file setup.sh reads: 'none'; absent = pi)."""
    try:
        return open(os.path.join(AGENT, "harness-agent")).read().strip() == "none"
    except OSError:
        return False


def ask_agent(reconfigure):
    """Ask once whether to wire pi; No = engine + sidecar only, for opencode or any OpenAI-compatible agent."""
    f = os.path.join(AGENT, "harness-agent")
    if (os.path.exists(f) and not reconfigure) or not sys.stdin.isatty():
        return
    on = ask_yn("Wire pi as your coding agent? No = engine + sidecar only; point opencode or another "
                "OpenAI-compatible agent at the endpoint", True)
    os.makedirs(AGENT, exist_ok=True)
    open(f, "w").write("pi\n" if on else "none\n")


def ask_once(key, question, reconfigure):
    """y/N asked on a terminal, then remembered; --reconfigure asks again."""
    st = jget(EXTRAS, {})
    if (key in st and not reconfigure) or not sys.stdin.isatty():
        return bool(st.get(key))
    st[key] = ask_yn(question)
    jput(EXTRAS, st)
    return st[key]


def serena_languages(root="."):
    """Enable serena language servers for the languages found in the project; -> languages added."""
    yml = os.path.join(root, ".serena", "project.yml")
    if not os.path.exists(yml):
        return []
    n = {}
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if d not in (".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".serena")]
        for f in fn:
            lang = LANGS.get(os.path.splitext(f)[1])
            if lang:
                n[lang] = n.get(lang, 0) + 1
        if sum(n.values()) > 5000:
            break
    want = [l for l, c in n.items() if c >= 3]
    text = open(yml).read()
    m = re.search(r"^language_servers:\n((?:- .*\n)+)", text, re.M)
    if not m:
        return []
    have = [x[2:].strip() for x in m.group(1).splitlines()]
    add = [l for l in want if l not in have]
    if add:
        open(yml, "w").write(text[:m.end(1)] + "".join(f"- {l}\n" for l in add) + text[m.end(1):])
    return add


def git(*a, timeout=15):
    r = subprocess.run(["git", "-C", REPO, *a], capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout.strip()


def harness_update_offer():
    """If this checkout is behind its upstream, offer a fast-forward pull, then restart the wizard on the new code."""
    if not sys.stdin.isatty() or os.environ.get("HARNESS_NO_UPDATE_CHECK") == "1":
        return
    try:
        if git("fetch", "--quiet", timeout=10)[0] or git("status", "--porcelain")[1]:
            return  # offline / no remote, or local edits: stay quiet (the /harness-update command explains)
        rc, n = git("rev-list", "--count", "HEAD..@{u}")
        if rc or not n.isdigit() or int(n) == 0:
            return
        print(f"harness is {n} commit(s) behind origin:")
        print(git("log", "--oneline", "-8", "HEAD..@{u}")[1])
        if not ask_yn("Update now? git pull --ff-only, then setup restarts"):
            return
        if git("pull", "--ff-only", "--quiet", timeout=60)[0]:
            return print("! pull failed (diverged from origin?): run git pull yourself")
        print("updated; restarting setup on the new code\n")
        os.execv(sys.executable, [sys.executable, "-u", *sys.argv])
    except (subprocess.TimeoutExpired, OSError):
        return


UPDATE_PREF = os.path.join(AGENT, "harness-update-check")  # same answer file setup.sh and start.sh use


def update_pref():
    """'1' / '0' / '' (never asked); HALOGEN_CHECK_UPDATES=1|0 overrides (name kept for old setups)."""
    try:
        return os.environ.get("HALOGEN_CHECK_UPDATES") or open(UPDATE_PREF).read().strip()
    except OSError:
        return ""


def _ver(s):
    """'Strata 0.1.44.1' / 'v0.40.6' / 'b6312-abc' -> (kind, numbers) to compare, or None."""
    m = re.search(r"\bb(\d{3,})\b|(\d+(?:\.\d+)+)", str(s or ""))
    return None if not m else ("b", (int(m.group(1)),)) if m.group(1) else ("v", tuple(map(int, m.group(2).split("."))))


def pin_value(name, default):
    """scripts/lib-pins.sh in Python: the stored upgrade pin if it was made on top of this launcher default, else the default."""
    try:
        kv = dict(l.split("=", 1) for l in open(os.path.join(AGENT, "harness-pins")).read().splitlines() if "=" in l)
    except OSError:
        return default
    return kv[name] if kv.get(name) and kv.get(f"BASE_{name}") == default else default


def engine_have(engine):
    """The version of engine that is installed here (profiles.json engines.<e>.update.have), or '' when it has none / cannot be told."""
    u = PROFILES["engines"].get(engine, {}).get("update")  # .get: a custom endpoint has no profile entry
    if not u:
        return ""
    # "A | B": the first source that tells (Strata: its Docker image, else its source checkout)
    return next((v for v in (_have(engine, s.split()) for s in u["have"].split(" | ")) if v), "")


def _have(engine, spec):
    kind, *rest = spec
    if kind == "image":  # the image our launcher runs (--print-image): its own version label, '' until it is pulled
        img = subprocess.run(["bash", os.path.join(REPO, rest[0]), "--print-image"], capture_output=True, text=True).stdout.strip()
        v = tool("docker", "image", "inspect", "-f", '{{index .Config.Labels "org.opencontainers.image.version"}}', img).stdout.strip() if img else ""
        return "" if v == "<no value>" else v
    if kind == "pin":  # version pinned in our launcher: known even while the engine is down
        # "pin FILE DEFAULT_VAR PIN_NAME": the launcher's tracked DEFAULT_VAR=0.19.1, unless an upgrade stored a newer pin on top of it
        return pin_value(rest[2], re.search(rf"^{rest[1]}=([0-9.]+)", open(os.path.join(REPO, rest[0])).read(), re.M).group(1))
    if kind == "dir":  # a git checkout our launcher starts (Strata, osmos): its tag, known while the engine is down
        src = subprocess.run(["bash", os.path.join(REPO, rest[0]), "--print-dir"], capture_output=True, text=True).stdout.strip()
        return subprocess.run(["git", "-C", src, "describe", "--tags"], capture_output=True, text=True).stdout.strip()
    return json.load(http(f"http://127.0.0.1:{PROFILES['engines'][engine]['port']}{kind}"))[rest[0]]  # what the running server reports


def installed_version(engine):
    """engine_have for a menu row: 'v0.11.3', or '' when it has none, cannot be told, or the engine is down and only
    its server reports it (llama-server, vLLM, Ollama)."""
    try:
        v = str(engine_have(engine) or "").split()
    except Exception:
        return ""
    v = v[-1] if v else ""
    return f"v{v}" if v[:1].isdigit() else v


def engine_update(engine):
    """-> one line if a newer release of engine exists; '' when current, unknown or offline. Never installs."""
    u = PROFILES["engines"].get(engine, {}).get("update")
    if not u:
        return ""
    try:
        have = engine_have(engine)
        src, repo = u["latest"].split(":", 1)
        if src in ("gh", "ghrev"):
            new = json.load(http(f"https://api.github.com/repos/{repo}/releases/latest"))["tag_name"]
        if src == "ghrev":  # strix-llama.cpp: releases (v1.1.0), but images are tagged by build: does the image's commit contain it?
            img = subprocess.run(["bash", os.path.join(REPO, u["have"].split()[1]), "--print-image"], capture_output=True, text=True).stdout.strip()
            rev = tool("docker", "image", "inspect", "-f", '{{index .Config.Labels "org.opencontainers.image.revision"}}', img).stdout.strip()
            if json.load(http(f"https://api.github.com/repos/{repo}/compare/{new}...{rev}"))["status"] in ("ahead", "identical"):
                return ""
            return f"{ename(engine)} {new} available (running {have}): {u['how']}"
        if src == "ghcr":  # highest x.y.z tag
            tok = json.load(http(f"https://ghcr.io/token?scope=repository:{repo}:pull"))["token"]
            req = urllib.request.Request(f"https://ghcr.io/v2/{repo}/tags/list?n=1000", headers={"Authorization": f"Bearer {tok}"})
            tags = [t for t in json.load(urllib.request.urlopen(req, timeout=3))["tags"] if re.fullmatch(r"\d+\.\d+\.\d+", t)]
            new = max(tags, key=_ver)
    except Exception:
        return ""
    a, b = _ver(have), _ver(new)
    if not a or not b or a[0] != b[0] or b[1] <= a[1]:  # different schemes (llama.cpp b1234 vs v0.7.0): can't tell, stay quiet
        return ""
    return f"{ename(engine)} {new} available (running {str(have).split()[-1]}): {u['how'].format(new=new.lstrip('v'))}"


def upgrader(eng):
    """scripts/upgrade-<engine>.sh (halogen, gufo, strata, osmos, llamacpp): pull, restart, check, pin, roll back on failure. -> path or None."""
    p = os.path.join(REPO, "scripts", f"upgrade-{eng}.sh")
    return p if os.path.exists(p) else None


def update_offer(eng):
    """Ask once whether to check for engine updates, then check eng. -> True if the user wants the safe
    upgrade (engines with an upgrader; run later, after all questions); others get a notice with the command."""
    pref = update_pref()
    if not pref and sys.stdin.isatty():
        pref = "1" if ask_yn("Check for engine updates on setup/start? one short network call; never installs without asking") else "0"
        os.makedirs(AGENT, exist_ok=True)
        open(UPDATE_PREF, "w").write(pref + "\n")
    if pref != "1" or not sys.stdin.isatty():
        return False
    msg = engine_update(eng)
    if msg:
        notice(msg)
        return bool(upgrader(eng)) and ask_yn(f"Upgrade {ename(eng)} now? restarts the server, rolls back on failure")
    return False


def monitor_cmd(eng, url=None, once=False):
    """argv of the live monitor for an engine, or None. Strata: its own `strata monitor` (Textual, optional: Strata's
    venv has it when its setup installed the monitor requirements; --once needs none), else our scripts/halogen-top.sh
    --strata. halogen: halogen-top.sh. Other engines have none."""
    top = os.path.join(REPO, "scripts", "halogen-top.sh")
    if eng == "halogen":
        return ["bash", top, *([url] if url else [])]
    if eng != "strata":
        return None
    d = subprocess.run(["bash", os.path.join(REPO, "server", "start-strata.sh"), "--print-dir"], capture_output=True, text=True).stdout.strip()
    py = os.path.join(d, ".venv", "bin", "python")
    if d and os.path.isfile(os.path.join(d, "strata.py")) and os.access(py, os.X_OK) and (
            once or subprocess.run([py, "-c", "import textual"], capture_output=True).returncode == 0):
        return [py, os.path.join(d, "strata.py"), "monitor", "--url", url or f"http://127.0.0.1:{PROFILES['engines']['strata']['port']}", *(["--once"] if once else [])]
    return ["bash", top, "--strata", *([url] if url else [])]


def cmd_monitor(a):
    eng = a.engine or (active() or {}).get("engine") or next((x["engine"] for x in detect()["engines"]), "halogen")
    argv = monitor_cmd(eng, a.url, a.once)
    if not argv:
        return speed_tail(a.once, PROFILES["engines"].get(eng, {}).get("provider", eng))
    os.execvp(argv[0], argv)


SPEED_LOG = os.path.join(AGENT, "harness-speed.jsonl")  # extensions/harness-status writes one line per reply


def speed_row(r):
    """One reply as pi's footer shows it ('12:34:56  speed pf 1.2k/s (9.0k tok, 7.5s) · 41 t/s'), None when it has no rate
    (one burst, or too short): the footer skips those too. Keep in step with speedText in extensions/harness-status."""
    if not (r.get("pf_tps") or r.get("tps")):
        return None
    k = lambda n: f"{n / 1000:.1f}k" if n >= 1000 else str(int(n + 0.5))  # noqa: E731
    parts = ([f"pf {k(r['pf_tps'])}/s ({k(r['prompt'])} tok, {r['ttft']:.1f}s)"] if r.get("pf_tps") else []) + (
        [f"{k(r['tps'])} t/s"] if r.get("tps") else [])
    return f"{r['t'][11:19]}  speed {' · '.join(parts)}"


def speed_tail(once=False, provider=None):
    """Engines without a metrics endpoint: per-reply speeds pi measured client-side for provider (None: all); the last 10,
    then follow."""
    print(f"{provider or 'every'} reply speed timed by pi (prefill includes queueing, so it reads a little low): {SPEED_LOG}")
    if not os.path.exists(SPEED_LOG):
        print("no replies logged yet: start pi (./start.sh ~/your-project) and send a prompt")
        if once:
            return
        open(SPEED_LOG, "a").close()
    def row(line):
        try:
            r = json.loads(line)
            return speed_row(r) if provider in (None, r.get("provider")) else None
        except (ValueError, KeyError, TypeError):
            return None  # a line cut mid-write or from an older format
    f = open(SPEED_LOG)
    every = f.readlines()
    lines, follow, seen = [x for x in every if row(x)][-10:], not once, every[-1] if every else None
    try:
        while True:
            for line in lines:
                if s := row(line):
                    print(s, flush=True)
            if not follow:
                return
            lines = [line] if (line := f.readline()) else []
            if lines:
                seen = line
            elif os.path.exists(SPEED_LOG) and os.stat(SPEED_LOG).st_ino != os.fstat(f.fileno()).st_ino:
                # harness-status rotated the log (renamed a trimmed copy in): carry on after the last line read
                f.close()
                f = open(SPEED_LOG)
                every = f.readlines()
                lines = every[every.index(seen) + 1:] if seen in every else every
                seen = every[-1] if every else seen
            else:
                time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        f.close()


def saved_engine():
    """The engine ./setup.sh chose, or None on a box that was never set up. A pre-wizard halogen install has no profile but has the NPU extension."""
    p = active()
    if p:
        return p["engine"]
    return "halogen" if os.path.exists(os.path.join(AGENT, "extensions", "npu-retrieval.ts")) else None


def cmd_refresh(_):
    """Re-run the installer quietly for the engine already chosen (what /harness-update does after a pull). Never picks an engine."""
    eng = saved_engine()
    if not eng:
        sys.exit("not set up yet: run ./setup.sh first")
    sys.exit(0 if install_extensions(eng, verbose=False, show_ok=False, launch=False) else 1)


def cmd_up(_):
    """start.sh: bring the saved engine up (start it if we ship a launcher; ask before stopping another engine
    that holds the GPU memory), refresh the install quietly, then the update notice + upgrade offer."""
    eng = saved_engine()
    if not eng:
        sys.exit("not set up yet: run ./setup.sh first (nothing was started or installed)")
    ep = custom_endpoint()
    if ep:  # a custom endpoint is not ours to start: say if it is not answering, refresh the install, done
        if not served_models(0, base=ep["url"], key=ep["key"]):
            print(f"! custom endpoint {ep['url']} is not answering; {'continuing' if no_pi() else 'pi opens anyway'}", file=sys.stderr)
        if not install_extensions(CUSTOM, verbose=False, show_ok=False):
            print(f"! setup.sh reported problems (details: {SETUP_LOG})", file=sys.stderr)
        return
    running = {x["engine"]: x for x in detect()["engines"]}
    if eng not in running:
        plan = plan_start(eng, running, ask_start=False)
        if plan is None or not start_engine(eng, running, plan):
            print(f"! {not_answering(eng)}; {'continuing' if no_pi() else 'pi opens anyway'}", file=sys.stderr)
    if not install_extensions(eng, verbose=False, show_ok=False):
        print(f"! setup.sh reported problems (details: {SETUP_LOG})", file=sys.stderr)
    sync_ctx(eng)
    if no_pi():
        print(f"{ename(eng)}: http://127.0.0.1:{PROFILES['engines'][eng]['port']}/v1  (pi not wired; ./harness config to change)")
    if update_pref() == "1":
        msg = engine_update(eng)
        if msg:
            notice(msg)
            if upgrader(eng) and ask_yn(f"Upgrade {ename(eng)} now? restarts the server, rolls back on failure"):
                if subprocess.run(["bash", upgrader(eng)]).returncode:
                    print("! upgrade failed; continuing on the pinned version", file=sys.stderr)


def cmd_update_check(a):
    """start.sh: one line if the active (or named) engine has a newer release; silent when off/offline."""
    if update_pref() == "1":
        msg = engine_update(a.engine or saved_engine() or "halogen")
        if msg:
            notice(msg)


def notice(msg):
    """Update notices in bold yellow on a terminal, plain when piped (start.sh colours its own copy)."""
    print(paint("1;33", msg))


def engine_title(eng, mod):
    """`engine · model` for people: an engine that serves one model names it (osmos: DeepSeek-V4.1-Flash (osmos-evo)), the rest name
    the model they run. The raw id stays what the server and pi use."""
    cfg = PROFILES["engines"].get(eng, {})
    entry = catalog().get(mod or "") or (catalog()[catalog_id(mod)] if eng in ("gufo", "strata", "osmos") and mod else {})  # gufo serves qwen3.8-27b-q8 for qwen38-27b-q8
    pack = eng == "osmos" and eng in entry.get("engines", ())  # osmos-inkling-2-mini: a pack named in the catalog; else its DeepSeek model_label
    name = (pack and entry["label"].split(" (")[0]) or cfg.get("model_label") or (entry.get("label") or mod or "").split(" (")[0]  # catalog labels carry a parenthesis of notes
    return f"{ename(eng)} · {name or 'no model'}"


def versioned_title(eng, mod):
    """engine_title with the installed version after the engine (osmos v0.11.3 · DeepSeek-...), for the screens that show it."""
    t, v = engine_title(eng, mod), installed_version(eng)
    return t.replace(" · ", f" {v} · ", 1) if v else t


def choices_items(eng=None):
    """Every saved answer as its own item. An engine that serves one model only (osmos: DeepSeek-V4.1-Flash) names it, so the
    saved preference for the Qwen engines is not shown as what osmos runs."""
    ink = eng == "osmos" and osmos_inkling()
    model = model_name("inkling2-mini") if ink else PROFILES["engines"].get(eng or "", {}).get("model_label") or model_pref()
    # the weights answer means nothing to a model without an abliterated build (the 27B, Qwen3.6, Inkling)
    weights = [] if ink or eng in ("gufo", "strata", "llamacpp") and engine_model(eng) not in ABLITERATED else [f"weights {weights_pref()}"]
    return [f"model {model}", *weights, f"sidecar {'off' if sidecar_pref() == '0' else 'on'}",
            f"voice {'on' if jget(EXTRAS, {}).get('voice') else 'off'}", f"update check {'on' if update_pref() == '1' else 'off'}"]


def choices_line(eng=None):
    """One line with every saved answer, for the config menu and the wizard's steps list."""
    return " · ".join(choices_items(eng))


def paint(code, text):
    """ANSI colour for a terminal only; NO_COLOR (no-color.org) and piped output get plain text."""
    if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        return text
    return f"\033[{code}m{text}\033[0m"


def summary_row(label, text, width=None):
    """One labelled row of the wizard summary. With a width it wraps at word boundaries (a list passes its own break points)
    and every continuation keeps the label column; without one (piped output) it stays one line. The label is dimmed after
    wrapping, so the escape codes never count toward the width."""
    lead = f"  {label:<9}"
    units = text if isinstance(text, list) else text.split()
    if width is None:
        return lead + " ".join(units)
    lines, line = [], None
    for u in units:
        if line is None:
            line = lead + u
        elif len(line) + 1 + len(u) > width:
            lines.append(line)
            line = " " * len(lead) + u
        else:
            line += " " + u
    lines = lines + [line or lead]
    lines[0] = "  " + paint("2", f"{label:<9}") + lines[0][len(lead):]
    return "\n".join(lines)


SETUP_LOG = os.path.join(AGENT, "setup.log")


def launch_offer(eng, prov, mid):
    """End of the wizard: start pi right here instead of printing a command to copy."""
    if not (sys.stdin.isatty() and shutil.which("pi")):
        return
    cwd = os.getcwd()
    if cwd == REPO:
        print("  (you are in the harness checkout; start it on a project with: ./start.sh ~/your-project)")
        return
    if ask_yn(f"Start pi in {cwd} now?", True):
        os.environ["PONYTAIL_HIDE_STATUS"] = "1"  # same as start.sh
        os.execvp("pi", ["pi", "--provider", prov, "--model", mid])


CUSTOM = "custom"  # the engine name of a profile written by ./harness endpoint: any OpenAI-compatible server, no port table, no launcher


def custom_endpoint():
    """{url, provider, key} when the saved profile is a custom endpoint, else None. The key stays in models.json, where pi reads it."""
    p = active() or {}
    if p.get("engine") != CUSTOM or not p.get("url"):
        return None
    key = (((jget(os.path.join(AGENT, "models.json"), {}) or {}).get("providers") or {}).get(p.get("provider")) or {}).get("apiKey")
    return {"url": p["url"], "provider": p["provider"], "key": None if key in (None, "", "dummy") else key}


def live_reply(eng, model_id, timeout=120):
    """Ask the engine itself for a few tokens over HTTP (no agent needed) -> (ok, detail)."""
    ep = custom_endpoint() if eng == CUSTOM else None
    port = "" if ep else PROFILES["engines"][eng]["port"]
    base, key, where = (ep["url"], ep["key"], ep["url"]) if ep else (f"http://127.0.0.1:{port}", engine_key(eng), f":{port}")
    body = {"model": model_id, "stream": True, "max_tokens": 64, "temperature": 0,
            "messages": [{"role": "user", "content": "Reply with exactly: OK"}]}
    t0 = time.time()
    try:
        with http(f"{base}/v1/chat/completions", body, timeout=timeout, key=key) as r:
            for line in r:
                line = line.decode().strip()
                if not line.startswith("data:") or line.endswith("[DONE]"):
                    continue
                for c in json.loads(line[5:]).get("choices") or []:
                    d = c.get("delta") or {}
                    if d.get("content") or d.get("reasoning_content") or d.get("reasoning"):  # a thinking model may answer in reasoning first
                        return True, f"{ename(eng)} answered in {time.time() - t0:.1f}s"
    except urllib.error.HTTPError as e:
        return False, f"{ename(eng)} on {where} returned HTTP {e.code} for model {model_id}" + (" (wrong or missing key?)" if e.code in (401, 403) else "")
    except (OSError, ValueError) as e:
        return False, f"no reply from {ename(eng)} on {where} ({e})"
    return False, f"{ename(eng)} on {where} sent no text"


def cmd_verify(a):
    """./harness verify [engine] [model]: a live reply from the engine; exit 0 only if it answered."""
    p = active() or {}
    eng, mod = a.engine or p.get("engine"), a.model or p.get("model")
    if not eng or (eng not in PROFILES["engines"] and eng != CUSTOM) or not mod:
        sys.exit("fail: nothing configured to check (run ./harness setup)")
    ok, msg = live_reply(eng, mod)
    print(msg)
    sys.exit(0 if ok else 1)


def probe_endpoint(url, key):
    """GET <url>/v1/models -> ([model dicts], '') or ([], why)."""
    try:
        return json.load(http(url + "/v1/models", timeout=10, key=key))["data"], ""
    except urllib.error.HTTPError as e:
        return [], f"HTTP {e.code}" + (" - this server wants a key: --key, or HARNESS_ENDPOINT_KEY in the environment" if e.code in (401, 403) else "")
    except (OSError, ValueError, LookupError) as e:
        return [], str(e)


def cmd_endpoint(a):
    """./harness endpoint URL [--name N] [--key K] [--model ID] [--ctx N]: use any OpenAI-compatible server (another machine, a
    non-default port, LM Studio/Ollama/vLLM elsewhere) as the main engine. Nothing is started or stopped; pi gets a provider with
    the URL, key and context window, and the saved profile remembers the URL so status, verify and the doctor can reach it."""
    import getpass
    url = a.url.strip().rstrip("/")
    url = url[:-3] if url.endswith("/v1") else url
    if not re.fullmatch(r"https?://[^/\s]+(/\S*)?", url):
        sys.exit(f"not a URL: {a.url!r} (want http://host:port or https://host/prefix; /v1 is added for you)")
    name = a.name or CUSTOM
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,30}", name):
        sys.exit(f"--name {name!r}: lower-case letters, digits, - and _ (it names the pi provider)")
    if name in PROFILES["engines"] or name == "llamacpp-tiny":
        sys.exit(f"--name {name}: that is one of this harness' own engines/providers; pick another (default: {CUSTOM})")
    key = a.key or os.environ.get("HARNESS_ENDPOINT_KEY") or None
    models, why = probe_endpoint(url, key)
    if why.startswith("HTTP 40") and not key and sys.stdin.isatty():
        key = getpass.getpass("  this server wants an API key (not echoed): ").strip() or None
        models, why = probe_endpoint(url, key)
    if not models and not (a.model and a.ctx):
        sys.exit(f"{url}/v1/models did not answer ({why or 'no models listed'}). Fix that, or give both --model and --ctx to configure it anyway.")
    ids = [m["id"] for m in models]
    mid = a.model or (choose("Which model?", ids, False) if len(ids) > 1 and sys.stdin.isatty() else ids[0])
    if len(ids) > 1 and not a.model and not sys.stdin.isatty():
        print(f"  {len(ids)} models listed ({', '.join(ids[:6])}{', ...' if len(ids) > 6 else ''}); using {mid} - pick another with --model")
    mdl = next((m for m in models if m["id"] == mid), {})
    ctx = a.ctx or next((int(x) for x in (mdl.get("meta", {}).get("n_ctx"), mdl.get("context_length"), mdl.get("max_model_len"), mdl.get("max_context_length")) if x), 0) \
        or (server_ctx(0, url, key) if models else 0)
    if not ctx and sys.stdin.isatty():
        ans = input("  the server does not report its context window. Tokens? [32768]: ").strip()
        ctx = int(ans) if ans.isdigit() else 0
    if not ctx:
        print(f"  ! the context window is unknown; assuming 32768. Set it with --ctx N (or contextWindow for {mid} in models.json)")
        ctx = 32768
    inputs = mdl.get("architecture", {}).get("input_modalities") or ["text"]
    mj = os.path.join(AGENT, "models.json")
    m = jget(mj, {})
    if os.path.exists(mj) and not os.path.exists(mj + ".pre-harness"):
        shutil.copy(mj, mj + ".pre-harness")
    prov = entries.build_provider(url + "/v1", name, mid, ctx, inputs, PROFILES, name=f"{mid} ({name})")
    prov["apiKey"] = key or "dummy"  # pi sends this as the bearer token
    m.setdefault("providers", {})[name] = prov  # an explicit command replaces an earlier entry of the same name
    m["providers"].setdefault("llamacpp-tiny", entries.tiny_provider(PROFILES))
    jput(mj, m)
    if key:
        os.chmod(mj, 0o600)  # the key is a secret: owner only, like pi's auth.json
    sj = os.path.join(AGENT, "settings.json")
    if os.path.exists(sj) and not os.path.exists(sj + ".pre-harness"):
        shutil.copy(sj, sj + ".pre-harness")
    s = jget(sj, {})
    s["defaultProvider"], s["defaultModel"] = name, mid
    s.setdefault("compaction", {}).update({"enabled": True, "reserveTokens": reserve_for(ctx)})
    jput(sj, s)
    jput(os.path.join(AGENT, "harness-profile.json"), {"engine": CUSTOM, "provider": name, "model": mid, "url": url, "npu": False,
                                                     "npuUrl": "http://127.0.0.1:8731", "sidecarUrl": "http://127.0.0.1:8090"})
    print(f"  endpoint {url}/v1 -> pi provider '{name}', model {mid}, context {ctx}{', key saved in models.json' if key else ''}")
    if not install_extensions(CUSTOM, verbose=False, show_ok=False, launch=False):
        print(f"! setup.sh reported problems (details: {SETUP_LOG})")
    ok, msg = live_reply(CUSTOM, mid, timeout=120)
    print(("  ✓ " if ok else "  ✗ ") + msg)
    if not ok:
        sys.exit(1)
    print("  ready: start pi in a project with ./start.sh (the sidecar and the NPU tools are not part of a custom endpoint)")


def run_doctor():
    """./setup.sh --doctor, quietly -> (passed, its failing lines). The doctor's exit code is the verdict; it includes the live reply."""
    r = subprocess.run(["bash", os.path.join(REPO, "setup.sh"), "--doctor"], capture_output=True, text=True)
    return r.returncode == 0, [l.strip() for l in r.stdout.splitlines() if "\u2717" in l]


def verified(eng):
    """Last step of setup: the doctor (health + live reply). Prints what failed; True only if it passed."""
    print(f"\n== checking: ./setup.sh --doctor (includes a live reply from {ename(eng)}) ==")
    passed, bad = run_doctor()
    for l in bad or ([] if passed else ["\u2717 the doctor failed - run ./setup.sh --doctor for details"]):
        print("  ", l)
    return passed


def finish(eng, mod, no_tune, launch=False):
    tuned = jget(os.path.join(AGENT, "harness-tuned.json"), {}).get(hw_key(eng, mod), {}) if no_tune else {}
    if not no_tune:
        print(f"== probing {engine_title(eng, mod)} (prefill + decode, <2 min) ==")
        tuned = tune(eng, mod)
        if "error" in tuned:
            print(f"  probe failed ({tuned['error']}) - keeping shipped defaults")
            tuned = {}
        else:
            print(f"  {tuned['prefill_tps']} t/s prefill (8K prompt), {tuned['decode_tps']} t/s decode (short code), median of {tuned['samples']}")
            sp = tuned["spread"]["decode_code"]
            if tuned["decode_tps"] and sp > 0.15 * tuned["decode_tps"]:
                print(f"  ! decode varied by {sp} t/s between runs: if other apps were busy, close them and re-run ./harness tune")
    if no_pi():
        e = PROFILES["engines"][eng]
        passed = verified(eng)
        print(f"\n== {'ready' if passed else 'NOT ready'} (pi not wired) ==\n  engine   {ename(eng)}: http://127.0.0.1:{e['port']}/v1  (OpenAI-compatible, key: any)\n  sidecar  http://127.0.0.1:8090/v1")
        print("  re-enable pi: ./setup.sh --pi   (then ./setup.sh)")
        jput(os.path.join(AGENT, "harness-profile.json"), {"engine": eng, "provider": e["provider"], "model": mod,
                                                           "npu": bool(e["npu"]), "npuUrl": "http://127.0.0.1:8731", "sidecarUrl": "http://127.0.0.1:8090"})  # so start.sh knows the engine
        if not passed:
            sys.exit(1)
        return
    prov, mid, ctx = apply(eng, mod, tuned)
    speed = f"{tuned['prefill_tps']} t/s prefill, {tuned['decode_tps']} t/s decode (short code)" if tuned.get("decode_tps") else "not probed (./harness tune)"
    if no_tune and tuned.get("decode_tps"):
        speed += f" (saved {tuned.get('when', '')}; re-measure: ./harness tune)"
    if eng in WEIGHTS_ASK and catalog_id(mod) in ABLITERATED:  # the other weights' last result beside this one
        other = {w: v for w, v in speeds(eng, mod).items() if w not in ("?", weights_pref())}
        speed = f"{weights_pref()}: {speed}" + "".join(f"; {w}: {v['prefill_tps']} t/s prefill, {v['decode_tps']} t/s decode "
                                                       f"({v.get('when', '?')})" for w, v in other.items())
    passed = verified(eng)
    width = shutil.get_terminal_size().columns if sys.stdout.isatty() else None  # narrow windows wrap; piped output stays one line
    items = choices_items(eng)
    head = f"== {'ready' if passed else 'NOT ready'} =="
    print("\n" + paint("1;32" if passed else "1;31", head))
    print(summary_row("engine", [versioned_title(eng, mod), f"(pi: {prov}/{mid})"], width))
    print(summary_row("context", str(ctx), width))
    print(summary_row("speed", speed, width))
    mem = gufo_memory(mod) if eng == "gufo" else ""
    if mem:
        print(summary_row("memory", mem, width))
    print(summary_row("start", "./start.sh ~/your-project", width))
    print(summary_row("monitor", "./harness monitor (live speed, memory and requests; Ctrl-C to quit)" if eng in ("halogen", "strata")
                      else "./harness monitor (prefill and decode of each reply, timed by pi; also in pi's footer)", width))
    print(summary_row("measure", "./harness tune (1 min quick probe) · ./harness bench (comparable bench-v1 runs)", width))
    print(summary_row("choices", [f"{it} ·" for it in items[:-1]] + [items[-1], "(change: ./harness config)"], width))
    print(summary_row("check", "./setup.sh --doctor (status: ./harness status)", width))
    if not passed:
        sys.exit(1)
    if launch:
        launch_offer(eng, prov, mid)


# every saved answer the wizard's questions can write, so declining "Continue?" (or Esc / Ctrl-C before it) really changes nothing
ANSWER_FILES = ("harness-agent", "harness-sidecar", "harness-extras.json", "harness-update-check", "harness-weights",
                "harness-model", "harness-quant", "harness-pick.json")
UNDO = None  # the answers as they were before the questions; cleared once the user says Continue


def snapshot_answers():
    snap = {}
    for f in ANSWER_FILES:
        try:
            snap[f] = open(os.path.join(AGENT, f)).read()
        except OSError:
            snap[f] = None
    return snap


def restore_answers(snap):
    for f, txt in snap.items():
        p = os.path.join(AGENT, f)
        if txt is None:
            if os.path.exists(p):
                os.remove(p)
        else:
            open(p, "w").write(txt)


def cmd_setup(a):
    global UNDO
    UNDO = snapshot_answers() if not a.yes and sys.stdin.isatty() else None
    d = detect()
    h = d["hardware"]
    verbose = a.verbose or active() is None  # first run shows everything; later runs only what needs you
    print(f"hardware: {h['ram_gb']} GB RAM, gpu={h['gpu']}, npu={h['npu']}; engines up: {[x['engine'] for x in d['engines']] or 'none'}")
    cap = capacity()
    if a.yes or not sys.stdin.isatty() or "Strix Halo" not in cap.get("gpu_name", ""):  # Strix Halo: the model menu's first row is the recommendation
        print("\n".join(describe(recommend(h, cap), cap)[1:]))
    # 1. every question first ...
    if not a.yes:
        # the update offer may exec the wizard on the new code (no `finally`: UNDO never runs), so it goes first: after it,
        # the snapshot taken above still covers every answer written below
        harness_update_offer()
        ask_agent(a.reconfigure)
    if not d["pi"] and not no_pi():
        print("! pi not found on PATH - install it first")
    up = []  # the update check runs as soon as the engine is chosen, before "Start <engine>?"
    eng, plan = pick_engine(d, a.yes, on_pick=None if a.yes else lambda e: up.append(update_offer(e)) or up[0])
    upgrade = bool(up and up[0])
    if not a.yes:
        ask_sidecar()
    if not sidecar_pref() and not sidecar_default():  # not asked (--yes, no terminal) on a small card: do not reserve 8 GiB of it unasked
        set_sidecar(False)
    voice = not a.yes and ask_once("voice", "Set up voice? local STT/TTS via Lemonade", a.reconfigure)
    if not a.yes and sys.stdin.isatty():
        steps = [("skip pi wiring (engine + sidecar only)" if no_pi() else f"install/update pi extensions, providers, skills{'' if verbose else f' (details: {SETUP_LOG})'}"),
                 choices_line(eng)] + (
            [f"stop {', '.join(map(ename, plan['stop']))} and the ling-tiny sidecar"] if plan and plan["stop"] else []) + (
            ["download halogen (~124 GB, resumable)"] if plan and plan["download"] else []) + (
            [f"install Strata with {catalog()[engine_model('strata')]['label']} (its setup downloads ~{catalog()[engine_model('strata')]['download_gb']} GB, resumable)" if engine_model("strata") != DEFAULT_MODEL else
             f"install Strata for the {weights_pref()} weights (" + ("orca-uncensored in place" if weights_pref() == "abliterated" else "shared stock UD-Q4_K_XL in place") + ")"] if plan and plan.get("install") == "strata" else []) + (
            [f"download {catalog()[plan['g27']]['label'].split(' (')[0]} (~{plan.get('g27_gb', catalog()[plan['g27']]['download_gb'])} GB, resumable)"] if plan and plan.get("g27") else []) + (
            [f"download osmos's Inkling 2 Mini pack (~{catalog()['inkling2-mini']['download_gb']} GB)"] if plan and plan.get("osmos_pack") else []) + (
            [f"download the stock {plan['stock']} (~{quant_gb(plan['stock'])} GB, resumable)"] if plan and plan.get("stock") else []) + (
            [f"download the abliterated {ename(eng)} weights (~{plan.get('weights_gb')} GB, resumable)"] if plan and plan.get("weights") else []) + (
            [f"upgrade {ename(eng)} (restart, rolls back on failure)"] if upgrade else []) + (
            [f"start {ename(eng)} (asks which model once it answers, if it serves several)"] if plan else [f"use {ename(eng)} (running)"]) + (
            [] if a.no_tune else ["probe speed (<2 min; skipped if already measured for this model, unless upgraded)"])
        print("\nabout to:\n" + "".join(textwrap.fill(s, term_width(), initial_indent="  - ", subsequent_indent="    ") + "\n" for s in steps))
        if not ask_yn("Continue?", True):
            sys.exit("nothing changed")  # main() puts the answers back
    UNDO = None
    # 2. ... then run without stopping: install, free memory/download, upgrade, and start the engine last
    if not install_extensions(eng, ["--voice"] if voice else [], verbose, launch=False):
        print(f"! setup.sh reported problems (see above{'' if verbose else ' and ' + SETUP_LOG})")
    added = [] if no_pi() else serena_languages()
    if added:
        print(f"serena: enabled language servers {added} in .serena/project.yml (restart serena to load them)")
    stopped = plan and plan.get("stop")
    if plan and not prepare_start(plan, {x["engine"]: x for x in d["engines"]}):
        sys.exit(f"\n! a step before starting {ename(eng)} failed (output above); nothing was started." + (f" {', '.join(map(ename, stopped))} was stopped for it: ./start.sh brings the saved engine back." if stopped else "")
                 + " Re-run ./setup.sh to resume: downloads continue where they stopped.")
    if upgrade:  # a gufo upgrade needs halogen already stopped; the upgraders start/restart the server themselves
        print(f"\n== upgrading {ename(eng)} ==")
        if subprocess.run(["bash", upgrader(eng)]).returncode:
            print("! upgrade failed; continuing on the pinned version")
        d["engines"] = detect()["engines"]
        if plan and served_models(PROFILES["engines"][eng]["port"], eng):
            plan = None  # the upgrade left it running: nothing to start
    eng, mod = bring_up(d, eng, plan, a.yes)
    saved = jget(os.path.join(AGENT, "harness-tuned.json"), {}).get(hw_key(eng, mod), {}).get("decode_tps")
    finish(eng, mod, a.no_tune or (bool(saved) and not upgrade), launch=not a.yes)


def cmd_use(a):
    eng, mod = pick_target(detect(), True, a.target)
    install_extensions(eng)
    finish(eng, mod, a.no_tune or bool(jget(os.path.join(AGENT, "harness-tuned.json"), {}).get(hw_key(eng, mod))))


def cmd_tune(_):
    p = active() or sys.exit("no active profile: run ./harness setup")
    if p["engine"] == CUSTOM:
        sys.exit("./harness tune measures the engines this harness starts; a custom endpoint is not measured (set --ctx when you add it)")
    finish(p["engine"], p["model"], False)


def cmd_status(_):
    p = active()
    if not p:
        return print("not configured: run ./harness setup")
    ep = custom_endpoint()
    if ep:
        up = bool(served_models(0, base=ep["url"], key=ep["key"]))
        return print(f"engine custom {ep['url']} {'UP' if up else 'DOWN'}  model {p['model']}  provider {ep['provider']}  (a custom endpoint: not started, stopped or tuned by this harness)")
    port = PROFILES["engines"][p["engine"]]["port"]
    up = bool(served_models(port, p["engine"]))
    print(f"engine {versioned_title(p['engine'], p['model'])} :{port} {'UP' if up else 'DOWN'}  npu {p['npu']}")
    print("speed:", speed_text(p["engine"], p["model"]) or "not measured yet (./harness tune)")
    print(f"memory: {mem_line({e['engine']: e['port'] for e in running_engines()}, sidecar_up())})")
    if sidecar_pref() == "0":
        return print("sidecar: off (opted out; ./harness config turns it on)")
    try:
        http(p["sidecarUrl"] + "/health")
        print("sidecar: UP")
    except Exception:
        print("sidecar: down (starts on demand via ling-tiny.service)")


def redact(s):
    """A report is pasted into a public issue: the home folder becomes ~ and the user name goes."""
    home = os.path.expanduser("~")
    s = s.replace(home, "~") if len(home) > 1 else s
    user = os.environ.get("USER") or os.path.basename(home)
    return re.sub(rf"\b{re.escape(user)}\b", "<user>", s) if len(user) > 2 else s


def report_text(doctor=True):
    """What a bug report needs, as plain lines: versions, hardware, engine, model, context, measured speed, the doctor's failures.
    No prompts, no keys, no paths; the home folder and user name are redacted."""
    import platform
    p = active() or {}
    eng = p.get("engine")
    hw, cap = hardware(), capacity()

    def out(*cmd):
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=15).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""
    cpu = next((l.split(":", 1)[1].strip() for l in open("/proc/cpuinfo") if l.startswith("model name")), "") if os.path.exists("/proc/cpuinfo") else platform.processor()
    try:
        have = engine_have(eng) if eng else ""
    except Exception:
        have = ""
    port = PROFILES["engines"][eng]["port"] if eng in PROFILES["engines"] else 0
    tuned = jget(os.path.join(AGENT, "harness-tuned.json"), {}).get(hw_key(eng, p.get("model", "")), {}) if eng else {}
    up = [e["engine"] for e in detect()["engines"]]
    ep = custom_endpoint()
    if ep:  # remote: say it is one, never where (the host may be private)
        is_up = bool(served_models(0, base=ep["url"], key=ep["key"]))
        where, ctx = f"custom endpoint, {ep['url'].split('://')[0]}://<host>, {'up' if is_up else 'DOWN'}", (server_ctx(0, ep["url"], ep["key"]) if is_up else 0)
    else:
        where, ctx = f"port {port}, {'up' if eng in up else 'DOWN'}", (engine_ctx(eng) if eng in up else 0)
    L = ["local-ai-harness report (./harness report: no prompts, keys or paths; home folder and user name redacted)", "",
         f"harness:  {out('git', '-C', REPO, 'describe', '--tags', '--always', '--dirty') or 'unknown (not a git checkout)'}",
         f"OS:       {platform.platform()} / python {platform.python_version()}",
         f"CPU:      {cpu or 'unknown'} ({hw['cpus']} threads)",
         f"RAM:      {hw['ram_gb']} GB",
         f"GPU:      {cap['gpu_name'] or hw['gpu']}, {cap['vram_gb']:g} GB VRAM, {cap['fast_gb']:g} GB GPU-addressable",
         f"NPU:      {'yes' if hw['npu'] else 'no'}    disk free: {cap['disk_free_gb']} GB",
         f"engine:   {eng or 'not set up'}{' ' + have if have else ''} ({where}); local engines answering: {', '.join(up) or 'none'}",
         f"model:    {p.get('model', '-')}    context: {ctx or 'unknown'}    weights: {weights_pref() if eng in WEIGHTS_ENGINES else '-'}",
         f"speed:    {tuned.get('prefill_tps', '?')} t/s prefill, {tuned.get('decode_tps', '?')} t/s decode on short code (./harness tune, a quick probe; ./harness bench for comparable numbers, {tuned.get('when', 'not measured')})",
         f"pi:       {out('pi', '--version') or 'not found'}"]
    if doctor:
        passed, bad = run_doctor()
        L += [f"doctor:   {'all checks passed' if passed else str(len(bad) or 1) + ' problem(s)'}", *[f"            {b}" for b in bad]]
    return redact("\n".join(L))


def cmd_report(a):
    """Print a report to paste into a GitHub issue (.github/ISSUE_TEMPLATE/bug_report.yml asks for it)."""
    print(report_text(not a.no_doctor))


OSMOS_BUDGET_MODE = os.path.join(AGENT, "harness-osmos-budget")  # remember (default) | auto; server/osmos-budget.py reads it


def osmos_budget_mode():
    try:
        return open(OSMOS_BUDGET_MODE).read().strip() or "remember"
    except OSError:
        return "remember"


def osmos_budget_row():
    """The config row: the mode, and the newest remembered budget (server/osmos-budget.py keeps one per config)."""
    if osmos_budget_mode() != "remember":
        return "osmos memory  size every start  (~3 min; adapts to what else runs)"
    saved = sorted(jget(os.path.join(LOGDIR, "osmos-budget.json"), {}).values(), key=lambda v: v.get("when", ""))
    size = f" ({saved[-1]['budget_gib']:g} GiB)" if saved else ""
    return f"osmos memory  remember last size{size}  (skips ~3 min sizing)"


def model_name(m):
    return catalog().get(m, {}).get("label", m).split(" (")[0]


def short_model(m):
    """A menu label: the 27B quants get a short name, so a weights row stays inside an 80-column window."""
    lbl = model_name(m)
    return f"Qwen3.8 27B {'Q8' if m.endswith('-q8') else 'Q4'}" if "27B" in lbl else lbl


def abl_model(m):
    """The nearest model with an abliterated build: the 27B Q8 for a 27B, else Flash-Next."""
    return "qwen38-27b-q8" if is_27b(m) else DEFAULT_MODEL


def quant_of(m):
    """'UD-Q8_K_XL' from the catalog label ('Qwen3.8 27B dense, UD-Q8_K_XL + DFlash2 draft')."""
    return catalog().get(m, {}).get("label", "").partition(", ")[2].partition(" +")[0]


def switch_now(eng):
    """After a model change in ./harness config: download what is missing, (re)start eng on the new model, then rewire pi."""
    if not ask_yn(f"Switch {ename(eng)} to {model_name(engine_model(eng))} now? downloads what is missing, then (re)starts it", True):
        return print("  saved; ./setup.sh downloads what is missing and (re)starts it")
    running = {x["engine"]: x for x in detect()["engines"]}
    others = {k: v for k, v in running.items() if k != eng}
    plan = plan_start(eng, others, ask_start=False)  # download questions before anything stops; starting needs no question here
    if plan is None:
        return
    served = None
    if eng in running:        # strata: its served ids are not ours to map, so a changed model always restarts it
        if eng == "gufo" and not serves_other(eng, running):
            print(f"  {ename(eng)} already serves {engine_model(eng)}")
        elif not stop_engine(eng, running) or not start_engine(eng, others, plan):
            return
    elif not start_engine(eng, others, plan):
        return
    served = (served_models(PROFILES["engines"][eng]["port"], eng) or [""])[0]
    install_extensions(eng, verbose=False)
    finish(eng, served, bool(jget(os.path.join(AGENT, "harness-tuned.json"), {}).get(hw_key(eng, served))))


def cmd_config(a):
    """Menu of every saved answer; change one, applied right away."""
    if not sys.stdin.isatty():
        return print(choices_line((active() or {}).get("engine")))
    global IN_MENU
    IN_MENU = True
    hints = os.path.join(AGENT, "harness-hints-shown")
    while True:
        p = active() or {}
        upd = update_pref()
        sc = sidecar_pref() != "0"
        voice = bool(jget(EXTRAS, {}).get("voice"))
        eng = p.get("engine")
        own = eng and eng not in ("gufo", "strata", "llamacpp") and eng != CUSTOM  # serves its own model: the model answer is not its
        notes = []
        if eng and eng != CUSTOM:  # a row stays short (80 columns); the numbers go under the question
            notes.append(f"{ename(eng)} speed (./harness tune): {speed_text(eng, p.get('model')) or 'not measured yet'}")
        if own and eng != "osmos":  # osmos's model (DeepSeek or Inkling) comes from the model menu
            notes.append(f"{ename(eng)} runs {engine_title(eng, p.get('model')).split(' · ', 1)[1]} only; pick gufo, Strata or strix-llama.cpp under engine to choose a model")
        if eng == "osmos" and not osmos_inkling() and (weights_file("osmos") != "stock") != (weights_pref() == "abliterated"):  # osmos.json = its last start
            notes.append(f"! osmos was last started on the other weights; {weights_pref()} applies on its next start")
        no_abl = eng in ("gufo", "strata", "llamacpp") and engine_model(eng) not in ABLITERATED or eng == "osmos" and osmos_inkling()
        live = {e["engine"]: e["port"] for e in running_engines()}
        ours = [e for e in live if PROFILES["engines"][e].get("script")]  # vllm, ollama, lmstudio, your own llama-server: not ours to stop
        tiny = sidecar_up()
        items = {
            f"engine        {versioned_title(eng, p['model']) if eng else '-'}": "engine",
            f"running       {mem_line(live, tiny)}{', pick to stop' if ours or tiny else ''})": "running",
            **({f"model         {model_name(engine_model(eng))}": "model"}
               if eng in ("gufo", "strata", "llamacpp") else {}),  # only these load the model answer
            **({f"weights       {'stock' if no_abl else weights_pref()}  ({'this model has no abliterated build' if no_abl else 'every engine, next start'})": "weights"}
               if not eng or eng in WEIGHTS_ASK else {}),  # vllm, ollama, lmstudio, a custom endpoint: their weights are not ours
            f"agent         {'none (engine only)' if no_pi() else 'pi'}  (extensions, providers, MCP)": "agent",
            f"sidecar       {'on' if sc else 'off'}  (ling-tiny, ~8 GiB GPU memory)": "sidecar",
            f"voice         {'on' if voice else 'off'}  (Lemonade STT/TTS)": "voice",
            f"update check  {'on' if upd == '1' else 'off'}  (newer engine release; never installs)": "update",
            **({"sudo hints    shown once  (pick to show them again on the next ./setup.sh)": "hints"} if os.path.exists(hints) else {}),
            **({osmos_budget_row(): "osmos-budget"} if eng == "osmos" else {}),
            "done": "done"}
        k = items[choose("Change which setting?", list(items), False, esc="done", notes=notes)]  # Esc at the top closes quietly, like done
        if k == "done":
            return
        if k == "running":
            names = [ename(e) for e in ours] + (["the ling-tiny sidecar"] if tiny else [])
            if not names:
                print("  nothing the harness started is running")
            elif ask_yn(f"Stop {', '.join(names)}? frees the GPU memory; a pi session using it loses its engine until ./start.sh", True):
                for e in ours:
                    stop_engine(e, live)
                stop_sidecar()
                print(f"  stopped; {mem_after_stop()}")
        elif k == "engine":
            eng, mod = pick_target(detect(), False, esc=BACK)
            if eng is BACK:
                continue  # Esc in a submenu: back to "Change which setting?", nothing changed
            install_extensions(eng, verbose=False)
            finish(eng, mod, bool(jget(os.path.join(AGENT, "harness-tuned.json"), {}).get(hw_key(eng, mod))))
        elif k == "model":
            changed = ask_model(eng, back=True)
            if changed is BACK:
                continue
            if changed:
                switch_now(eng)
        elif k == "weights":
            eng = p.get("engine", "halogen")
            changed = ask_weights(eng if eng in WEIGHTS_ASK else "halogen", back=True)
            if no_abl and changed is True:
                to = abl_model(engine_model(eng))
                if ask_yn(f"{short_model(engine_model(eng))} has no abliterated build. Switch {ename(eng)} to {short_model(to)} abliterated?", True):
                    open(MODEL_FILE, "w").write(to + "\n")
                    switch_now(eng)
                else:
                    print("\n".join(textwrap.wrap(f"  saved; {ename(eng)} stays on {short_model(engine_model(eng))} (stock) until you pick "
                                                  f"{short_model(to)} under model", term_width(), subsequent_indent="    ")))
                continue
            if changed is BACK or not changed or eng not in WEIGHTS_ASK:
                continue
            running = {x["engine"]: x for x in detect()["engines"]}
            if ask_yn(f"{'Restart' if eng in running else 'Start'} {ename(eng)} on the {weights_pref()} weights now? downloads them first if missing", True):
                others = {k: v for k, v in running.items() if k != eng}
                plan = plan_start(eng, others, ask_start=False)  # download questions before anything stops
                if plan is None or (eng in running and not stop_engine(eng, running)):
                    continue
                start_engine(eng, others, plan)
        elif k == "agent":
            was_off = no_pi()  # read before open("w") truncates the file
            open(os.path.join(AGENT, "harness-agent"), "w").write("pi\n" if was_off else "none\n")
            if was_off:
                install_extensions(p.get("engine", "halogen"), verbose=False, launch=False)  # never start an engine from a toggle
                print("  pi wired; ./harness setup finishes the provider setup")
            else:
                print("  pi wiring off (nothing uninstalled; ./start.sh now only brings the engine up)")
        elif k == "sidecar":
            open(os.path.join(AGENT, "harness-sidecar"), "w").write("0\n" if sc else "1\n")
            r = tool("systemctl", "--user", "stop" if sc else "start", "ling-tiny.service")
            print(f"  sidecar {'off, stopped' if sc else 'on, started'}" if not r.returncode else
                  f"  saved; {'stop' if sc else 'start'} failed - run ./setup.sh to {'stop' if sc else 'install'} it")
        elif k == "voice":
            st = jget(EXTRAS, {})
            st["voice"] = not voice
            jput(EXTRAS, st)
            if voice:
                print("  voice off (nothing uninstalled; /speak and /talk stay available)")
            else:
                install_extensions((active() or {}).get("engine", "halogen"), ["--voice"], verbose=False)
        elif k == "osmos-budget":
            os.makedirs(AGENT, exist_ok=True)
            remember = osmos_budget_mode() == "remember"
            open(OSMOS_BUDGET_MODE, "w").write("auto\n" if remember else "remember\n")
            print("  osmos sizes its memory on every start (~3 min, adapts to what else runs)" if remember else
                  "  osmos reuses the last sized memory when as much is free (one build; sizes again when less is)")
        elif k == "update":
            os.makedirs(AGENT, exist_ok=True)
            open(UPDATE_PREF, "w").write("0\n" if upd == "1" else "1\n")
        elif k == "hints" and os.path.exists(hints):
            os.remove(hints)
            print("  sudo hints will show on the next ./setup.sh run")


def main():
    import argparse
    ap = argparse.ArgumentParser(prog="harness")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for n, f in (("detect", cmd_detect), ("setup", cmd_setup), ("use", cmd_use), ("tune", cmd_tune), ("bench", cmd_bench), ("status", cmd_status), ("config", cmd_config), ("update-check", cmd_update_check), ("up", cmd_up), ("recommend", cmd_recommend), ("quant-info", cmd_quant_info), ("ctx-info", cmd_ctx_info), ("model-info", cmd_model_info), ("monitor", cmd_monitor), ("verify", cmd_verify), ("refresh", cmd_refresh), ("report", cmd_report), ("endpoint", cmd_endpoint)):
        p = sub.add_parser(n)
        p.set_defaults(fn=f)
        if n in ("setup", "use"):
            p.add_argument("--yes", action="store_true")
            p.add_argument("--no-tune", action="store_true")
            p.add_argument("--reconfigure", action="store_true", help="ask the saved extras again (one at a time: ./harness config)")
            p.add_argument("--verbose", action="store_true", help="show full setup.sh output (default on the first run only)")
        if n == "use":
            p.add_argument("target", help="engine[/model]")
        if n == "bench":
            p.add_argument("--runs", type=int, default=3, help="repeats; the median is reported (default 3)")
            p.add_argument("--depths", default="2048,8192,32768", help="prompt depths in tokens (default 2048,8192,32768; those over half the window are skipped)")
        if n == "recommend":
            p.add_argument("--json", action="store_true")
        if n == "monitor":
            p.add_argument("engine", nargs="?", help="default: the active engine")
            p.add_argument("--url", help="server root, e.g. a remote Strata (default: this box)")
            p.add_argument("--once", action="store_true", help="print one snapshot and exit (Strata, and the per-reply log of other engines)")
        if n == "endpoint":
            p.add_argument("url", help="http://host:port (or https://host/prefix); /v1 is added")
            p.add_argument("--name", help=f"the pi provider's name (default: {CUSTOM})")
            p.add_argument("--key", help="API key (or set HARNESS_ENDPOINT_KEY: a flag shows in the process list and shell history)")
            p.add_argument("--model", help="model id (default: the server's first, or a menu when there are several)")
            p.add_argument("--ctx", type=int, help="context window in tokens (default: what the server reports, else asked)")
        if n == "report":
            p.add_argument("--no-doctor", action="store_true", help="skip the doctor run (a few seconds, includes a live reply from the engine)")
        if n == "verify":
            p.add_argument("engine", nargs="?", help="default: the active engine")
            p.add_argument("model", nargs="?", help="default: the active model")
        if n == "model-info":
            p.add_argument("id")
            p.add_argument("field")
        if n in ("quant-info", "ctx-info"):
            p.add_argument("name")
        if n == "update-check":
            p.add_argument("engine", nargs="?", help="default: the active engine")
    a = ap.parse_args()
    try:
        a.fn(a)
    except (KeyboardInterrupt, EOFError):
        cancel()
    finally:
        if UNDO is not None:  # left the wizard before "Continue?": put the saved answers back as they were
            restore_answers(UNDO)


if __name__ == "__main__":
    main()
