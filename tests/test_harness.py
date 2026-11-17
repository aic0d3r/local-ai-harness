"""python3 tests/test_harness.py - wizard core against a mock OpenAI server (no real engine needed)."""
import json, os, re, shutil, subprocess, sys, tempfile, threading, time, types, http.server

AGENT = tempfile.mkdtemp()
os.environ["PI_CODING_AGENT_DIR"] = AGENT
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "setup"))
import harness as h

PRISTINE = {n: getattr(h, n) for n in ("served_models", "server_ctx")}  # the tests below stub these; the last block needs the real ones


class Mock(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _j(self, o):
        b = json.dumps(o).encode(); self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        if self.path == "/v1/models": self._j({"data": [{"id": "Qwen3-8B", **({"owned_by": "gufo", "context_length": 65536, "architecture": {"input_modalities": ["text", "image"]}} if GUFO else {} if STRATA else {"owned_by": "llamacpp"})}]})
        elif self.path == "/props" and GUFO: self.send_response(400); self.end_headers()
        elif self.path == "/props": self._j({"default_generation_settings": {"n_ctx": 32768}})
        else: self._j({"ok": 1})
    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(200); self.end_headers()
        for c in ('{"choices":[{"delta":{"content":"x"}}]}', '{"choices":[],"usage":{"prompt_tokens":900}}', "[DONE]"):
            self.wfile.write(f"data: {c}\n\n".encode())


STRATA = GUFO = False
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Mock)
threading.Thread(target=srv.serve_forever, daemon=True).start()
h.PROFILES["engines"]["llamacpp"]["port"] = srv.server_port

assert h.model_profile("Qwen3-8B")["reasoning"] and h.model_profile("unknown-x")["maxTokens"] == 8192
assert h.server_ctx(srv.server_port) == 32768
d = h.detect()
assert [e["engine"] for e in d["engines"]].count("llamacpp") == 1, d["engines"]
t = h.tune("llamacpp", "Qwen3-8B")
assert t["prefill_tps"] > 0 and t["decode_tps"] > 0 and t["protocol"] == h.BENCH_ID and t["samples"] == 3, t
assert h.engine_title("osmos", "osmos-osmos-evo") == "osmos · DeepSeek-V4.1-Flash (osmos-evo)" and h.engine_title("llamacpp", "Qwen3-8B") == "llamacpp · Qwen3-8B" and h.engine_title("halogen", "flash-next") == "halogen · Qwen3.8-Flash-Next (halogen .hgn)"
assert h.engine_title("gufo", None) == "gufo · no model" and "/" not in h.engine_title("strata", "flash-next-iq3_s")
q = h.bench("llamacpp", "Qwen3-8B", runs=2, quick=True)  # tune is this quick subset: one prefill cell, decode repeats
assert set(q["median"]) == {"decode_code", "prefill_8192"} and q["quick"], q
# bench: fixed protocol, medians per cell, depths over half the window skipped (the mock reports n_ctx 32768)
b = h.bench("llamacpp", "Qwen3-8B", runs=2, depths=(2048, 65536))
assert b["protocol"] == "bench-v1" and set(b["median"]) == {"decode_code", "decode_prose", "prefill_2048", "decode_code_at_2048"}, b
assert b["median"]["prefill_2048"] > 0 and b["runs"] == 2
assert h.bench_text(100, "x") == h.bench_text(100, "x") != h.bench_text(100, "y")  # same text on every engine, new text per seed
prov, mid, ctx = h.apply("llamacpp", "Qwen3-8B", {"maxTokens": 4096})
m = json.load(open(f"{AGENT}/models.json"))["providers"]["llamacpp"]["models"][0]
assert (prov, mid, ctx) == ("llamacpp", "Qwen3-8B", 32768) and m["maxTokens"] == 4096 and "chatTemplateKwargs" in m["compat"]
assert json.load(open(f"{AGENT}/harness-profile.json"))["npu"] is False
assert "llamacpp-tiny" in json.load(open(f"{AGENT}/models.json"))["providers"]  # sidecar reachable by registry lookups
# Strata shares :8080 and also serves /props (seen on Strata 0.1.44) but no owned_by=llamacpp
STRATA = True
h.PROFILES["engines"]["strata"]["port"] = srv.server_port
up = [e["engine"] for e in h.detect()["engines"] if e["port"] == srv.server_port]  # real engines on this box may also be up
assert up == ["strata"], up
# gufo shares :8080: selectable by name once something answers there
h.PROFILES["engines"]["gufo"]["port"] = srv.server_port
assert h.pick_target(h.detect(), True, "gufo")[0] == "gufo"
# `use gufo` while gufo is down plans the start (stop the holder, download, start) instead of printing "stop halogen first"
gport = h.PROFILES["engines"]["gufo"]["port"]
h.PROFILES["engines"]["gufo"]["port"] = 1  # nothing answers there
real_plan = h.plan_start
h.plan_start = lambda e, r, ask_start=True: {"stop": "halogen", "download": False, "weights": None, "for": e}
assert h.pick_engine({"engines": []}, True, "gufo") == ("gufo", {"stop": "halogen", "download": False, "weights": None, "for": "gufo"})
h.plan_start = lambda e, r, ask_start=True: None  # the user declined
try:
    h.pick_engine({"engines": []}, True, "gufo")
    raise AssertionError("declined start must exit")
except SystemExit as e:
    assert "gufo is not answering" in str(e)
h.plan_start = real_plan
assert h.pick_engine({"engines": []}, True, "vllm") == ("vllm", None)  # no launcher: bring_up prints how to start it
h.PROFILES["engines"]["gufo"]["port"] = gport
# a real gufo (owned_by=gufo) is detected as gufo, not strata, and its context_length is read
GUFO = True
assert [e["engine"] for e in h.detect()["engines"] if e["port"] == srv.server_port] == ["gufo"]
assert h.server_ctx(srv.server_port) == 65536
assert h.server_input(srv.server_port) == ["text", "image"] and h.server_input(1) == ["text"]
# sync_ctx: a stale window and a launcher-owned reserve follow the served window; a /tune reserve that still fits stays
h.jput(f"{AGENT}/models.json", {"providers": {"gufo": {"models": [{"id": "Qwen3-8B", "contextWindow": 262144}]}}})
saved_profile = h.jget(f"{AGENT}/harness-profile.json")
h.jput(f"{AGENT}/harness-profile.json", {"engine": "gufo", "model": "Qwen3-8B"})
h.jput(f"{AGENT}/settings.json", {"compaction": {"reserveTokens": 60000}})
h.sync_ctx("gufo")
assert json.load(open(f"{AGENT}/models.json"))["providers"]["gufo"]["models"][0]["contextWindow"] == 65536
assert json.load(open(f"{AGENT}/settings.json"))["compaction"]["reserveTokens"] == 16384
h.jput(f"{AGENT}/settings.json", {"compaction": {"reserveTokens": 12345}})
h.sync_ctx("gufo")
assert json.load(open(f"{AGENT}/settings.json"))["compaction"]["reserveTokens"] == 12345
h.jput(f"{AGENT}/harness-profile.json", saved_profile)
# started gufo but detect names the port something else: keep the engine we started
d = {"engines": [{"engine": "strata", "port": srv.server_port, "models": ["m"]}]}
assert h.bring_up(d, "gufo", None, True) == ("gufo", "m")
GUFO = False
# halogen's NPU helper models are not offered as chat models
h.PROFILES["engines"]["vllm"]["hide"] = "^Qwen3"
assert h.served_models(srv.server_port, "vllm") == []
# serena: detect project languages and enable matching language servers (idempotent)
proj = tempfile.mkdtemp()
os.makedirs(f"{proj}/.serena")
open(f"{proj}/.serena/project.yml", "w").write('project_name: "x"\nlanguage_servers:\n- python\nencoding: "utf-8"\n')
for i in range(3):
    open(f"{proj}/a{i}.py", "w").write("x=1")
    open(f"{proj}/b{i}.ts", "w").write("let x=1")
open(f"{proj}/lone.go", "w").write("package x")  # one file: below the 3-file bar
assert h.serena_languages(proj) == ["typescript"] and h.serena_languages(proj) == []
assert "- typescript\nencoding" in open(f"{proj}/.serena/project.yml").read()
# extras answers: no terminal -> not asked, remembered answer used
assert h.ask_once("voice", "?", False) is False
# sidecar opt-out answer: not asked without a terminal; read back from the file setup.sh and the extensions share
assert h.sidecar_pref() == ""
h.ask_sidecar()
assert h.sidecar_pref() == ""
open(f"{AGENT}/harness-sidecar", "w").write("0\n")
assert h.sidecar_pref() == "0"
os.remove(f"{AGENT}/harness-sidecar")
# wizard summary on a narrow window: rows wrap at words within the width, continuations keep the label column, and choices only break after a ' ·'
items = ["model qwen3.8-flash-next", "weights stock", "sidecar on", "voice off", "update check on"]
row = h.summary_row("choices", [f"{it} ·" for it in items[:-1]] + [items[-1], "(change: ./harness config)"], 52).split("\n")
assert len(row) > 1 and all(len(l) <= 52 for l in row), row
assert all(l.startswith(" " * 11) and not l[11:].startswith("·") for l in row[1:]), row
assert " ".join(" ".join(row).split()) == "choices model qwen3.8-flash-next · weights stock · sidecar on · voice off · update check on (change: ./harness config)"
assert h.summary_row("speed", "1808 t/s prefill, 66.1 t/s decode (short code)") == "  speed    1808 t/s prefill, 66.1 t/s decode (short code)"
# colour: only on a terminal, off under NO_COLOR; the dimmed label does not change the wrap width
import io, re
class _Tty(io.StringIO):
    def isatty(self): return True
real_out = sys.stdout
sys.stdout = _Tty()
try:
    assert h.paint("1;32", "ok") == "\033[1;32mok\033[0m"
    os.environ["NO_COLOR"] = "1"
    assert h.paint("1;32", "ok") == "ok"
    del os.environ["NO_COLOR"]
    tty_row = h.summary_row("choices", [f"{it} ·" for it in items[:-1]] + [items[-1], "(change: ./harness config)"], 52)
    assert tty_row.startswith("  \033[2mchoices  \033[0mmodel"), tty_row
    plain = re.sub(r"\033\[[0-9;]*m", "", tty_row).split("\n")
    assert all(len(l) <= 52 for l in plain), plain
finally:
    sys.stdout = real_out
assert "\033[" not in h.summary_row("choices", items, 52)  # piped (this run) stays plain
# engine menu: running first, labels map back to engine names; engines without a launcher print how to start
run = {"strata": {"engine": "strata", "port": 1, "models": ["m"]}}
assert h.engine_menu(run) == "llamacpp"  # no tty: first entry, which is the saved current engine
run2 = dict(run)
assert h.heap_holder({"gufo": 1}) == "gufo" and h.heap_holder(run) == "strata" and h.heap_holder({"vllm": 1}) is None
assert h.start_engine("vllm", run) is False  # no launcher: prints how to start it
# quiet install: full setup.sh output goes to setup.log, the exit code still decides success
fake = tempfile.mkdtemp()
open(f"{fake}/setup.sh", "w").write('echo "== 1. a =="; printf "  \\033[32m✓\\033[0m fine\\n"; echo "== 2. b =="; echo "  ! careful"; exit 3\n')
real_repo, h.REPO = h.REPO, fake
assert h.install_extensions("llamacpp", verbose=False) is False
h.REPO = real_repo
assert "! careful" in open(h.SETUP_LOG).read()
# engine update check: versions compare across the formats engines report; different schemes never compare
assert h._ver("Strata 0.1.44.1") < h._ver("v0.1.45") and h._ver("b6312-abc") == ("b", (6312,)) and h._ver("") is None
assert h.engine_update("lmstudio") == ""  # no update source: silent
# osmos: the running version is the libosmos checkout's tag (start-osmos.sh --print-dir), the latest the GitHub release; the upgrader is found
co = tempfile.mkdtemp()
for g in ("init -q", "-c user.name=t -c user.email=t@t commit -q --allow-empty -m x", "tag v0.7.1"):
    subprocess.run(["git", "-C", co, *g.split()], check=True)
real_http, real_od = h.http, os.environ.get("OSMOS_DIR")
os.environ["OSMOS_DIR"] = co
for latest, want in (("v0.7.5", True), ("v0.7.1", False), ("v0.7.0", False)):
    h.http = lambda url, *a, _l=latest, **k: __import__("io").BytesIO(json.dumps({"tag_name": _l}).encode())
    msg = h.engine_update("osmos")
    assert bool(msg) == want and (not want or ("osmos v0.7.5 available (running v0.7.1)" in msg and "upgrade-osmos.sh" in msg)), (latest, msg)
h.http = real_http
if real_od is None: os.environ.pop("OSMOS_DIR")
else: os.environ["OSMOS_DIR"] = real_od
assert h.upgrader("osmos") and h.upgrader("osmos").endswith("upgrade-osmos.sh")
# osmos serves DeepSeek-V4.1-Flash only: the summary names that, never the saved Qwen preference; other engines still show the preference
open(h.MODEL_FILE, "w").write("qwen38-27b\n")
assert "DeepSeek-V4.1-Flash" in h.choices_line("osmos") and "qwen38-27b" not in h.choices_line("osmos")
assert "model qwen38-27b" in h.choices_line("gufo") and "model qwen38-27b" in h.choices_line()
# Strata serves its own ids (qwen3.6-35b-a3b-ud-q4_k_xl): the ready screen used to call that Qwen3.8-Flash-Next
assert h.catalog_id("qwen3.6-35b-a3b-ud-q4_k_xl") == "qwen36-q4" and "Qwen3.6-35B-A3B" in h.model_name(h.catalog_id("qwen3.6-35b-a3b-ud-q4_k_xl"))
# Qwen3.6-35B-A3B runs on llama.cpp (Docker, halo-box image) now, and the weights answer is moot for it
assert h.PROFILES["catalog"]["qwen36-q4"]["engines"][0] == "llamacpp" and h.PROFILES["engines"]["llamacpp"]["script"] == "server/start-qwen36.sh"
assert h.PROFILES["routes"]["qwen36"]["model"] == "qwen36-q4" and h.PROFILES["routes"]["qwen36"]["engines"][0]["engine"] == "llamacpp"
real_model = open(h.MODEL_FILE).read() if os.path.exists(h.MODEL_FILE) else "qwen38-27b\n"
open(h.MODEL_FILE, "w").write("qwen36-q4\n")
assert "weights" not in h.choices_line("llamacpp"), h.choices_line("llamacpp")  # no abliterated build for this model
open(h.MODEL_FILE, "w").write("flash-next\n")
assert "weights" in h.choices_line("llamacpp"), h.choices_line("llamacpp")
open(h.MODEL_FILE, "w").write(real_model)
# the llama.cpp launcher is one docker run: it names the GGUF it serves and the image, and says what is missing
lc = os.path.join(h.REPO, "server", "start-qwen36.sh")
assert subprocess.run(["bash", lc, "--print-image"], capture_output=True, text=True).stdout.startswith("ghcr.io/halo-box/strix-llama.cpp@sha256:")
assert subprocess.run(["bash", lc, "--print-model"], capture_output=True, text=True,
                      env=dict(os.environ, QWEN36_GGUF="/tmp/x.gguf")).stdout.strip() == "/tmp/x.gguf"
none_env = dict(os.environ, QWEN36_GGUF="", QWEN36_DIR="/nonexistent", HOME="/nonexistent")
assert subprocess.run(["bash", lc, "--print-model"], capture_output=True, text=True, env=none_env).stdout.strip() == ""
assert "not on disk" in subprocess.run(["bash", lc], capture_output=True, text=True, env=none_env).stderr
# Flash-Next on llama.cpp: the stock GGUF gufo shares, abliterated = a LoRA beside it; says which file is missing
fn_dir = tempfile.mkdtemp()
fn_env = dict(os.environ, LLAMACPP_FAMILY="flash-next", ENGINE_DIR=fn_dir, LLAMACPP_WEIGHTS="abliterated")
assert subprocess.run(["bash", lc, "--print-lora"], capture_output=True, text=True, env=fn_env).stdout.strip() == f"{fn_dir}/HUIHUI-V2-Qwen3.8-Flash-Next-LoRA-F32.gguf"
assert subprocess.run(["bash", lc, "--print-model"], capture_output=True, text=True, env=fn_env).stdout.strip().endswith("UD-Q4_K_XL/Qwen3.8-Flash-Next-UD-Q4_K_XL-00001-of-00004.gguf")
assert "shard 1 of 4 missing" in subprocess.run(["bash", lc], capture_output=True, text=True, env=fn_env).stderr
os.makedirs(f"{fn_dir}/UD-Q4_K_XL")
for i in range(1, 5):
    open(f"{fn_dir}/UD-Q4_K_XL/Qwen3.8-Flash-Next-UD-Q4_K_XL-0000{i}-of-00004.gguf", "w").close()
assert "download-abliterated.sh llamacpp" in subprocess.run(["bash", lc], capture_output=True, text=True, env=fn_env).stderr
assert subprocess.run(["bash", lc, "--print-lora"], capture_output=True, text=True, env=dict(fn_env, LLAMACPP_FAMILY="qwen36-q4")).stdout == ""
shutil.rmtree(fn_dir)
assert h.catalog_id("qwen3.8-flash-next-ud-q4_k_xl") == "flash-next" and h.catalog_id("qwen3.8-flash-next-iq3_xxs") == "flash-next-iq3_xxs"
assert "weights" in h.choices_line("gufo") and "weights" in h.choices_line("halogen")  # Q4 has an abliterated build too
assert h.hw_key("gufo", "m", "stock", "qwen38-27b") != h.hw_key("gufo", "m", "abliterated", "qwen38-27b") == h.hw_key("gufo", "m", "abliterated", "qwen38-27b")
open(h.MODEL_FILE, "w").write("qwen38-27b-q8\n")  # keyed apart from the Q4 as well
assert "weights" in h.choices_line("gufo") and h.hw_key("gufo", "m", "abliterated", "qwen38-27b-q8") != h.hw_key("gufo", "m", "abliterated", "qwen38-27b")
for fam, w, f in (("qwen38-27b", "stock", "/Qwen3.8-27B-UD-Q4_K_XL.gguf"), ("qwen38-27b", "abliterated", "/Huihui-Qwen3.8-27B-abliterated-UD-Q4_K_XL.gguf"),
                  ("qwen38-27b-q8", "stock", "/Qwen3.8-27B-UD-Q8_K_XL.gguf"), ("qwen38-27b-q8", "abliterated", "/Huihui-Qwen3.8-27B-abliterated-UD-Q8_K_XL.gguf")):
    assert subprocess.run(["bash", os.path.join(h.REPO, "server", "start-gufo-flashnext.sh"), "--print-model"], capture_output=True, text=True,
                          env=dict(os.environ, GUFO_FAMILY=fam, GUFO_WEIGHTS=w)).stdout.strip().endswith(f), (fam, w)
open(h.MODEL_FILE, "w").write("qwen38-27b\n")
# the weights menu on the 27B Q4 and Q8: stock and abliterated are both real files, so either pick is a change
open(h.WEIGHTS, "w").write("stock\n")
seen = []
real_choose, real_yn = h.choose, h.ask_yn
real_stdin, h.sys.stdin = h.sys.stdin, types.SimpleNamespace(isatty=lambda: True)
h.choose = lambda q, o, y, esc=None, notes=(): seen.clear() or seen.extend(o) or next(x for x in o if x.startswith("abliterated"))
assert h.ask_weights("gufo", back=True) is True and "abliterated UD-Q4_K_XL (huihui-ai" in " ".join(seen) and "Flash-Next" not in " ".join(seen), seen
h.choose = lambda q, o, y, esc=None, notes=(): seen.clear() or seen.extend(o) or next(x for x in o if x.startswith("stock"))
assert h.ask_weights("gufo", back=True) is True and h.weights_pref() == "stock" and "stock UD-Q4_K_XL (Unsloth)" in " ".join(seen)
open(h.MODEL_FILE, "w").write("qwen38-27b-q8\n")
h.choose = lambda q, o, y, esc=None, notes=(): seen.clear() or seen.extend(o) or next(x for x in o if x.startswith("abliterated"))
assert h.ask_weights("gufo", back=True) is True and "abliterated UD-Q8_K_XL (huihui-ai" in " ".join(seen), seen
assert h.abl_model("qwen36-q4") == "flash-next" and h.abl_model("qwen38-27b") == "qwen38-27b-q8"
h.choose, h.ask_yn = real_choose, real_yn
h.sys.stdin = real_stdin
os.remove(h.WEIGHTS)  # back to never asked, as the tests below expect
# ./harness use gufo with gufo up on another model: it asks, even though use passes yes=True (it used to keep the old model)
up = {"engines": [{"engine": "gufo", "port": 8080, "models": ["qwen3.8-27b"]}]}  # gufo serving the Q4
open(h.MODEL_FILE, "w").write("qwen38-27b-q8\n")  # saved: the Q8
asked = []
real_yn, real_stdin = h.ask_yn, h.sys.stdin
h.sys.stdin = types.SimpleNamespace(isatty=lambda: True)
h.ask_yn = lambda q, d=False: asked.append(q) or False
assert h.pick_engine(up, True, "gufo") == ("gufo", None) and asked, asked  # declined: warned, not restarted
h.ask_yn, h.sys.stdin = real_yn, real_stdin
open(h.MODEL_FILE, "w").write("qwen38-27b\n")  # saved Q4 == served Q4: no question
assert not h.serves_other("gufo", {x["engine"]: x for x in up["engines"]})
# switch_now (a model change in ./harness config): start it, restart it only where the change needs it, then rewire pi
calls = []
saved = {n: getattr(h, n) for n in ("detect", "plan_start", "start_engine", "stop_engine", "served_models", "install_extensions", "finish", "ask_yn")}
h.ask_yn = lambda q, d=False: True
h.plan_start = lambda e, r, ask_start=True: calls.append(f"plan ask_start={ask_start}") or {"stop": None, "download": False}
h.start_engine = lambda e, r, plan=None, wait_s=900: calls.append(f"start {e}") or True
h.stop_engine = lambda e, r: calls.append(f"stop {e}") or True
h.served_models = lambda port, e=None, base=None, key=None: ["qwen3.8-27b-q8"]
h.install_extensions = lambda e, verbose=False: calls.append("wire")
h.finish = lambda e, m, no_tune, launch=False: calls.append(f"finish {e} {m}")
open(h.MODEL_FILE, "w").write("qwen38-27b-q8\n")
for up, want in (({"engines": [{"engine": "gufo", "port": 8080, "models": ["qwen3.8-27b-q8"]}]}, ["plan ask_start=False", "wire", "finish gufo qwen3.8-27b-q8"]),
                 ({"engines": [{"engine": "gufo", "port": 8080, "models": ["qwen3.8-27b"]}]}, ["plan ask_start=False", "stop gufo", "start gufo", "wire"]),
                 ({"engines": [{"engine": "strata", "port": 8080, "models": ["flash-next"]}]}, ["plan ask_start=False", "stop strata", "start strata", "wire"])):
    calls.clear()
    h.detect = lambda up=up: up
    h.switch_now("gufo" if "gufo" in str(up) else "strata")
    assert calls[:len(want)] == want, calls
for n, f in saved.items():
    setattr(h, n, f)
# ready screen's memory line: gufo's own load log; another model's load says nothing
log = os.path.join(AGENT, "gufo-test.log")
open(log, "w").write("x [cache] schema=c event=configured sessions=2 capacity_bytes=21991874560\n"
                      "x [loader] event=load_completed model=qwen3.8-27b-q8 sessions=2 context_tokens=262144 gpu_device_used_mib=82787 gpu_device_total_mib=126976\n")
real_log, h.engine_log = h.engine_log, lambda e: log
m = h.gufo_memory("qwen3.8-27b-q8")
assert m.startswith("80.8 of 124.0 GiB") and "prompt cache 20.5" in m and "2 sessions x 262144 context" in m, m
assert h.gufo_memory("qwen3.8-27b") == ""
h.engine_log = real_log
os.remove(log)
os.remove(h.MODEL_FILE)
# ./harness report: the lines a bug report needs, and nothing private (home folder and user name are redacted)
real_home, real_user = os.environ.get("HOME"), os.environ.get("USER")
os.environ["HOME"], os.environ["USER"] = "/home/alice_example", "alice_example"
assert h.redact("/home/alice_example/models/x by alice_example") == "~/models/x by <user>"
assert h.redact("nothing here") == "nothing here"
rep = h.report_text(doctor=False)
for key in ("harness:", "OS:", "CPU:", "RAM:", "GPU:", "NPU:", "engine:", "model:", "speed:", "pi:"):
    assert f"\n{key}" in rep, (key, rep)
assert "alice_example" not in rep and "doctor:" not in rep
real_rd = h.run_doctor
h.run_doctor = lambda: (False, ["\u2717 halogen down /home/alice_example/x"])
rep = h.report_text(doctor=True)
assert "doctor:   1 problem(s)" in rep and "halogen down ~/x" in rep and "alice_example" not in rep, rep
h.run_doctor = real_rd
os.environ["HOME"], os.environ["USER"] = real_home, real_user
# status reports an opted-out sidecar as off, not down
open(f"{AGENT}/harness-sidecar", "w").write("0\n")
assert "sidecar off" in h.choices_line()
# weights: stock until answered; the launchers resolve the saved answer to the file they load; tuning is keyed per weights
assert h.weights_pref() == "stock" and h.ask_weights("halogen") is False and "weights stock" in h.choices_line()
# tune results: stock and abliterated apart, old ones (no "weights" field) told apart by their key; another model left out
r = lambda d, p, **kw: {"engine": "osmos", "model": "osmos-osmos-evo", "when": "2026-11-15", "protocol": h.BENCH_ID, "decode_tps": d, "prefill_tps": p, **kw}  # noqa: E731
h.jput(os.path.join(AGENT, "harness-tuned.json"), {h.hw_key("osmos", "osmos-osmos-evo", "stock"): r(33.5, 469),
                                                  h.hw_key("osmos", "osmos-osmos-evo", "abliterated"): r(34.3, 475),
                                                  "x": {**r(36.0, 405), "engine": "gufo", "model": "qwen3.8-27b", "weights": "stock"},
                                                  "y": {**r(35.0, 400), "engine": "gufo", "model": "qwen3.8-27b", "weights": "abliterated", "when": "2026-11-14"},
                                                  "z": {**r(16.0, 300), "engine": "gufo", "model": "qwen3.8-27b-q8"}})
assert set(h.speeds("osmos")) == {"stock", "abliterated"}, h.speeds("osmos")
assert "stock 33.5 t/s" in h.speed_text("osmos") and "abliterated 34.3 t/s" in h.speed_text("osmos")
assert h.speed_text("gufo", "flash-next") == "" and "36.0" in h.speed_text("gufo", "qwen38-27b")
# the 27B Q4 has an abliterated build too, so both results are labelled; the Q8 keeps its own apart from the Q4
assert h.speed_text("gufo", "qwen38-27b").startswith("abliterated 35.0 t/s") and "stock 36.0" in h.speed_text("gufo", "qwen38-27b"), h.speed_text("gufo", "qwen38-27b")
assert h.speed_text("gufo", "qwen38-27b-q8").startswith("16.0 t/s")
# a running gufo on another model than the saved answer needs a restart (Q4 up, Q8 saved)
open(h.MODEL_FILE, "w").write("qwen38-27b-q8\n")
up = lambda m: {"gufo": {"engine": "gufo", "port": 8080, "models": [m]}}  # noqa: E731
assert h.serves_other("gufo", up("qwen3.8-27b")) and not h.serves_other("gufo", up("qwen3.8-27b-q8")) and not h.serves_other("gufo", {})
open(h.MODEL_FILE, "w").write("flash-next\n")
assert h.serves_other("gufo", up("qwen3.8-27b")) and not h.serves_other("gufo", up("qwen3.8-flash-next")) and not h.serves_other("strata", up("x"))
os.remove(h.MODEL_FILE)
os.remove(os.path.join(AGENT, "harness-tuned.json"))
k_stock = h.hw_key("gufo", "m")
open(h.WEIGHTS, "w").write("abliterated\n")
assert h.weights_file("gufo").endswith("orca-uncensored/Qwen3.8-Flash-Next-Uncensored-Gufo-00001-of-00005.gguf")
assert h.weights_file("halogen").endswith("/qwen38-flash-next-v2-abliterated.hgn")
assert h.hw_key("gufo", "m") != k_stock and h.hw_key("strata", "m") == h.hw_key("strata", "m")
os.remove(h.WEIGHTS)
assert h.hw_key("gufo", "m") == k_stock
# the refusal canary is what makes the weights answer measurable: its classifier must call
# an apology a refusal and an answer that merely says "can't" a normal reply
rc = subprocess.run([sys.executable, os.path.join(os.path.dirname(__file__), "..", "bench", "refusal-canary.py"), "--self-test"], capture_output=True, text=True)
assert rc.returncode == 0 and "verdicts correct" in rc.stdout, rc.stdout + rc.stderr
os.remove(f"{AGENT}/harness-sidecar")
# setup order: update offered right after the engine pick (before any start question); then install (no launch),
# free memory, upgrade, start last; an upgrade forces a fresh probe even when a saved result exists
import argparse, types
calls, saved = [], {n: getattr(h, n) for n in ("pick_engine", "update_offer", "upgrader", "install_extensions", "prepare_start", "bring_up", "finish", "harness_update_offer", "ask_sidecar", "ask_once", "served_models")}
h.harness_update_offer = h.ask_sidecar = lambda *a: None
h.ask_once = lambda *a: False
h.pick_engine = lambda d, yes, want=None, on_pick=None: (on_pick("gufo"), calls.append("plan_start"), ("gufo", {"stop": "halogen", "download": False}))[2]
h.update_offer = lambda e: calls.append("update_offer") or True
h.upgrader = lambda e: "/bin/true"
h.install_extensions = lambda e, x, v, launch=True: calls.append(f"install launch={launch}") or True
h.prepare_start = lambda p, r: calls.append("prepare") or True
h.served_models = lambda *a: []
h.bring_up = lambda d, e, p, y: calls.append(f"start plan={p is not None}") or (e, "Qwen3-8B")
h.finish = lambda e, m, no_tune, launch=False: calls.append(f"probe={not no_tune}")
real_cap, h.capacity = h.capacity, lambda: {"gpu_name": "t", "vram_gb": 0, "fast_gb": 8, "disk_free_gb": 500}  # keeps lspci out of the recorded calls
real_run, h.subprocess.run = h.subprocess.run, lambda *a, **k: calls.append("upgrade" if a[0][0] == "bash" else " ".join(a[0][:3])) or types.SimpleNamespace(returncode=0)
h.cmd_setup(argparse.Namespace(yes=False, verbose=False, reconfigure=False, no_tune=False))
h.subprocess.run = real_run
h.capacity = real_cap
for n, f in saved.items(): setattr(h, n, f)
assert calls == ["update_offer", "plan_start", "install launch=False", "prepare", "upgrade", "start plan=True", "probe=True"], calls
# upgrade chosen: pick_engine must not ask "Start <engine>?" (declining it used to exit setup); without one it does
asked, saved = [], {n: getattr(h, n) for n in ("engine_menu", "plan_start", "ask_weights", "ask_yn", "ask_route")}
h.engine_menu = lambda r, esc=None: "halogen"
h.ask_route = lambda esc=None: None  # the model-first menu is tested below
h.ask_weights = lambda e, back=False: False
h.plan_start = lambda e, r, ask_start=True: asked.append(ask_start) or {"stop": None, "download": False}
real_stdin, h.sys.stdin = h.sys.stdin, types.SimpleNamespace(isatty=lambda: True)
for want in (True, False):
    h.pick_engine({"engines": []}, False, on_pick=lambda e: want)
# new weights default while halogen runs: restart = a start plan that stops halogen first; declined = keep it running
h.ask_weights, h.ask_yn = (lambda e, back=False: True), (lambda q, d=False: True)
hal = {"engines": [{"engine": "halogen", "port": 8731, "models": ["m"]}]}
assert h.pick_engine(hal, False)[1]["stop"] == "halogen" and asked[-1] is False
h.ask_yn = lambda q, d=False: False
assert h.pick_engine(hal, False) == ("halogen", None)
asked.pop()
h.sys.stdin = real_stdin
for n, f in saved.items(): setattr(h, n, f)
assert asked == [False, True], asked

# model-first routes: the best engine this box can run, first usable wins
R = h.PROFILES["routes"]
npu, plain = {"npu": True, "ram_gb": 121}, {"npu": False, "ram_gb": 121}
assert h.best_engine(R["flash-next"], npu)["engine"] == "halogen" and h.best_engine(R["flash-next"], plain)["engine"] == "gufo"
assert h.best_engine(R["flash-next"], {"npu": True, "ram_gb": 64})["engine"] == "gufo"  # halogen needs halogen_min_ram_gb
assert h.best_engine(R["qwen38-27b"], npu)["engine"] == "gufo" and h.best_engine(R["deepseek"], plain)["engine"] == "osmos"
assert all(e["engine"] in h.PROFILES["engines"] for r in R.values() if isinstance(r, dict) for e in r.get("engines", []))
# ask_route saves the model answer, keeps a saved quant of the same family, and offers "pick an engine" as the way out
saved = {n: getattr(h, n) for n in ("capacity", "hardware", "choose")}
h.capacity, h.hardware = (lambda: {"gpu_name": "Strix Halo"}), (lambda: plain)
real_stdin, h.sys.stdin = h.sys.stdin, types.SimpleNamespace(isatty=lambda: True)
routes_seen = []
first = lambda q, o, y, esc=None, notes=(): routes_seen.extend(o) or next(x for x in o if "27B" in x)
h.choose = first
assert h.ask_route() == ("gufo", "qwen38-27b") and h.model_pref() == "qwen38-27b"
# the 27B route asks its quant (Q4 / Q8 are their own catalog ids), saved answer first
h.choose = lambda q, o, y, esc=None, notes=(): routes_seen.extend(o) or next(x for x in o if ("Q8" in x if "Which Qwen3.8-27B" in q else "27B" in x))
assert h.ask_route() == ("gufo", "qwen38-27b-q8") and h.model_pref() == "qwen38-27b-q8"
h.choose = lambda q, o, y, esc=None, notes=(): o[0]
assert h.ask_variant("qwen38-27b", "gufo") == "qwen38-27b-q8"  # saved one listed first
h.choose = first
h.choose = lambda q, o, y, esc=None, notes=(): next(x for x in o if "Flash-Next" in x)
assert h.ask_route() == ("gufo", "flash-next") and h.model_pref() == "flash-next"
open(h.MODEL_FILE, "w").write("flash-next-iq3_s\n")
assert h.ask_route() == ("gufo", "flash-next") and h.model_pref() == "flash-next-iq3_s"
h.choose = lambda q, o, y, esc=None, notes=(): next(x for x in o if "DeepSeek" in x)
assert h.ask_route() == ("osmos", None) and h.model_pref() == "flash-next-iq3_s"
h.choose = lambda q, o, y, esc=None, notes=(): o[-1]
assert h.ask_route() is None
# gum cuts a label at the window edge and a too-wide one drops the menu to numbers: rows fit 80 columns, detail goes in notes
assert all(len(o) + 2 <= 80 for o in routes_seen), [o for o in routes_seen if len(o) + 2 > 80]
h.capacity = lambda: {"gpu_name": "NVIDIA RTX 4090"}
assert h.ask_route() is None  # other hardware keeps the engine menu
h.sys.stdin = real_stdin
for n, f in saved.items(): setattr(h, n, f)
os.remove(h.MODEL_FILE)

# gufo abliterated: weights present but no shared MTP head -> the sidecar download is still owed
root = tempfile.mkdtemp(); os.makedirs(f"{root}/orca"); open(f"{root}/orca/w.gguf", "w").close()
real_wf, h.weights_file = h.weights_file, lambda e: f"{root}/orca/w.gguf"
assert h.weights_download_gb("gufo") == 5 and h.weights_download_gb("halogen") == 0
open(f"{root}/mtp-Qwen3.8-Flash-Next-shared-Q8_0.gguf", "w").close()
assert h.weights_download_gb("gufo") == 0
h.weights_file = lambda e: f"{root}/none.gguf"
assert h.weights_download_gb("gufo") == 112
# osmos: libosmos's installer reuses or fetches its own patch; the harness used to ask and then run a script that rejects osmos
assert h.weights_download_gb("osmos") == 0
h.weights_file = real_wf
# strata missing: plan_start asks to install it, prepare_start runs install-strata.sh before the start
saved = {n: getattr(h, n) for n in ("strata_installed", "ask_yn", "heap_holder")}
h.strata_installed, h.ask_yn, h.heap_holder = (lambda: False), (lambda q, d=False: True), (lambda r: None)
h.stock_ready = lambda *a: True  # the stock GGUF is on disk (this box's real state must not decide the test)
plan = h.plan_start("strata", {})
assert plan["install"] == "strata" and not plan.get("stock"), plan
ran, real_run = [], h.subprocess.run
h.subprocess.run = lambda a, **k: ran.append(os.path.basename(a[1])) or types.SimpleNamespace(returncode=0)
assert h.prepare_start(plan, {}) and ran == ["install-strata.sh"] and "install" not in plan
h.subprocess.run = real_run
# abliterated Strata runs gufo's shards: when they are missing the plan downloads them first (before the install), declining stops it
saved_w = {n: getattr(h, n) for n in ("weights_pref", "weights_file")}
h.weights_pref, h.weights_file = (lambda: "abliterated"), (lambda e: "/nonexistent/orca.gguf")
plan = h.plan_start("strata", {})
assert plan["install"] == "strata" and plan["weights"] == "gufo" and plan["weights_gb"] == 112, plan
ran = []
h.subprocess.run = lambda a, **k: ran.append(os.path.basename(a[1])) or types.SimpleNamespace(returncode=0)
assert h.prepare_start(plan, {}) and ran == ["download-abliterated.sh", "install-strata.sh"], ran
h.subprocess.run = real_run
h.ask_yn = lambda q, d=False: "orca-uncensored" not in q
assert h.plan_start("strata", {}) is None
h.ask_yn = lambda q, d=False: True
for n, f in saved_w.items(): setattr(h, n, f)
# stock weights missing: gufo and Strata both download the one shared UD-Q4_K_XL first (before any Strata install); declining stops it
h.stock_ready = lambda *a: False
plan = h.plan_start("gufo", {})
assert plan["stock"] == "UD-Q4_K_XL" and "install" not in plan, plan
plan = h.plan_start("strata", {})
assert plan["stock"] == "UD-Q4_K_XL" and plan["install"] == "strata", plan
ran = []
h.subprocess.run = lambda a, **k: ran.append(os.path.basename(a[1])) or types.SimpleNamespace(returncode=0)
assert h.prepare_start(plan, {}) and ran == ["download-stock.sh", "install-strata.sh"], ran
h.subprocess.run = real_run
h.ask_yn = lambda q, d=False: "UD-Q4_K_XL" not in q
assert h.plan_start("gufo", {}) is None
h.ask_yn = lambda q, d=False: True
h.stock_ready = lambda *a: True
assert not h.plan_start("gufo", {}).get("stock")
for n, f in saved.items(): setattr(h, n, f)
assert h.heap_holder({"strata": 1}) == "strata" and "tabbyapi" not in h.PROFILES["engines"]
# an NVIDIA box lists strata before the Strix-oriented engines (menu order on a fresh box)
real_hw, real_choose = h.hardware, h.choose
h.hardware = lambda: {"cpus": 8, "ram_gb": 64, "gpu": "nvidia", "npu": False}
seen = []
h.choose = lambda q, opts, yes, esc=None, notes=(): seen.extend(opts) or opts[0]
h.engine_menu({})
assert all(len(o) + 2 <= 80 for o in seen), [o for o in seen if len(o) + 2 > 80]
names = [o.split()[1] if o[2:3] != " " else o.split()[1] for o in seen]
assert names.index("strata") < names.index("gufo"), names
h.hardware, h.choose = real_hw, real_choose
# Strata keys its repacked dense weights by model name, so a start on the other weights answer must be refused, not run on the wrong pack
sd = tempfile.mkdtemp()
for f in ("setup.py", "update.sh"): open(f"{sd}/{f}", "w").close()
open(f"{sd}/run-unsloth-ud-q4_k_xl.sh", "w").write("#!/bin/sh\necho started\n")
strata = os.path.join(os.path.dirname(__file__), "..", "server", "start-strata.sh")
def srun(w, *a):
    return subprocess.run(["bash", strata, *a], capture_output=True, text=True, env=dict(os.environ, STRATA_DIR=sd, STRATA_WEIGHTS=w))
assert srun("abliterated", "--print-run").stdout.strip().endswith("run-unsloth-ud-q4_k_xl.sh")  # installs older than the marker were abliterated
assert srun("stock", "--print-run").stdout.strip() == ""
r = srun("stock"); assert r.returncode == 1 and "built from the abliterated weights" in r.stderr, r
open(f"{sd}/.harness-weights", "w").write("stock\n")
assert srun("stock", "--print-run").stdout.strip().endswith("run-unsloth-ud-q4_k_xl.sh") and srun("abliterated", "--print-run").stdout.strip() == ""
assert srun("stock").returncode == 0 and srun("abliterated").returncode == 1
# hardware -> engine/quant/context. The measured Strix Halo 124 GB box gets halogen; the rest is the table's arithmetic
R = h.PROFILES["recommend"]
rec = lambda gpu, ram, fast, vram=0, npu=False, name="Strix Halo": h.recommend({"gpu": gpu, "ram_gb": ram, "npu": npu}, {"fast_gb": fast, "vram_gb": vram, "gpu_name": name})
r = rec("amd", 124, 124.5, npu=True)
assert r["engine"] == "halogen" and r["tested"], r
r = rec("amd", 124, 124.5)          # same box without the NPU: gufo at the shipped default quant and context
assert (r["engine"], r["quant"], r["ctx"], r["tested"]) == ("gufo", "UD-Q4_K_XL", 262144, False), r
assert (rec("amd", 102, 102)["quant"], rec("amd", 102, 102)["ctx"]) == ("UD-Q3_K_XL", 262144)
assert (rec("amd", 96, 96)["quant"], rec("amd", 96, 96)["ctx"]) == ("UD-Q3_K_XL", 65536)
assert (rec("amd", 92, 92)["quant"], rec("amd", 92, 92)["ctx"]) == ("UD-IQ3_XXS", 65536)
assert [h.gufo_ctx(q, {"fast_gb": 124.5}) for q in ("UD-Q4_K_XL", "UD-Q3_K_XL")] == [262144, 262144] and h.gufo_ctx("UD-Q3_K_XL", {"fast_gb": 96}) == 65536
assert h.gufo_ctx("UD-Q5_K_XL", {"fast_gb": 124.5}) == 65536 and h.gufo_ctx("nope", {"fast_gb": 96}) == 262144  # no fit -> smallest tier; unknown quant -> max
assert rec("amd", 88, 88)["engine"] is None and rec("amd", 64, 64)["engine"] is None and rec("none", 128, 128, name="")["engine"] is None
# every other AMD/NVIDIA box goes to Strata (gufo is Strix Halo only); the model is the biggest ISTA pack whose RAM need fits (Strata's rule),
# Qwen3.6 below that, nothing under its 32 GB; abliterated needs the Unsloth layout; nothing here is measured
nv = lambda ram, vram=24, gpu="nvidia": rec(gpu, ram, vram, vram=vram, name="RTX 4090")
r = nv(64)
assert (r["engine"], r["model"], r["tested"]) == ("strata", "flash-next-iq3_s", False), r
assert [nv(x)["model"] for x in (56, 48, 44, 40, 32)] == ["flash-next-iq3_xxs", "flash-next-iq2_xs", "flash-next-q2_0", "qwen36-q4", "qwen36-q4"]
assert nv(30)["engine"] is None and nv(64, gpu="amd")["engine"] == "strata" and rec("apple", 64, 64, name="")["engine"] is None
assert all(m in h.catalog() for m in R["strata_order"])
h.weights_pref, real_wp = (lambda: "abliterated"), h.weights_pref
assert nv(64)["model"] == "flash-next" and nv(76, 8)["engine"] is None and nv(80, 8)["engine"] == "strata"  # Strata: ~88 GB keeps every UD-Q4_K_XL expert
h.weights_pref = real_wp
# the ladder is what the gufo launcher can load: every quant resolves to <quant>/...-00001-of-<shards>, sizes feed the download
for q in R["quants"]:
    out = subprocess.run(["bash", os.path.join(os.path.dirname(__file__), "..", "server", "start-gufo-flashnext.sh"), "--print-model"], capture_output=True, text=True,
                         env=dict(os.environ, GUFO_WEIGHTS="stock", GUFO_QUANT=q["name"])).stdout.strip()
    assert re.search(rf"/{q['name']}/Qwen3\.8-Flash-Next-{q['name']}-00001-of-0000[46]\.gguf$", out), out
    assert h.quant_gb(q["name"]) == int(q["gib"]) + R["extras_gb"] + 1
assert h.quant_gb("nope") is None
# accepting the gufo pick saves the quant (the launcher reads it) and the context (start_engine passes it); a repeat does not ask again
saved = {n: getattr(h, n) for n in ("hardware", "capacity", "ask_yn", "PICK", "QUANT_FILE", "MODEL_FILE")}
h.hardware, h.capacity = (lambda: {"gpu": "amd", "ram_gb": 96, "npu": False, "cpus": 8}), (lambda: {"gpu_name": "Strix Halo", "vram_gb": 0, "fast_gb": 96, "disk_free_gb": 500})
h.PICK, h.QUANT_FILE, h.MODEL_FILE = f"{AGENT}/pick.json", f"{AGENT}/quant", f"{AGENT}/model"
asked = []
h.ask_yn = lambda q, d=False: asked.append(q) or True
real_stdin, sys.stdin = sys.stdin, types.SimpleNamespace(isatty=lambda: True)
h.offer_recommendation("gufo")
assert open(h.QUANT_FILE).read().strip() == "UD-Q3_K_XL" and json.load(open(h.PICK)) == {"engine": "gufo", "model": "flash-next", "quant": "UD-Q3_K_XL", "ctx": 65536}, asked
assert h.saved_quant("gufo") == "UD-Q3_K_XL" and h.saved_quant("strata") == "UD-Q4_K_XL"
h.offer_recommendation("gufo"); h.offer_recommendation("strata")
assert len(asked) == 1, asked
assert h.hw_key("gufo", "m") != h.hw_key("strata", "m")
# a discrete-GPU box: accepting the Strata pick saves its model; engines that cannot load it keep Flash-Next; the model joins the tune key
h.hardware, h.capacity = (lambda: {"gpu": "nvidia", "ram_gb": 64, "npu": False, "cpus": 8}), (lambda: {"gpu_name": "RTX 4090", "vram_gb": 24, "fast_gb": 24, "disk_free_gb": 500})
k_before = h.hw_key("strata", "m")
assert h.offer_recommendation("strata") is True and h.model_pref() == "flash-next-iq3_s" and h.engine_model("strata") == "flash-next-iq3_s"
assert h.engine_model("gufo") == h.engine_model("halogen") == "flash-next" and h.hw_key("strata", "m") != k_before and h.hw_key("gufo", "m") == h.hw_key("gufo", "m")
n_asked = len(asked); assert h.offer_recommendation("strata") is True and len(asked) == n_asked  # same answer: not asked again
sys.stdin = real_stdin
for n, f in saved.items(): setattr(h, n, f)
# another model than Flash-Next: Strata installs it through its own setup (no stock/abliterated downloads); gufo's 27B fetches its own files
saved = {n: getattr(h, n) for n in ("MODEL_FILE", "strata_installed", "ask_yn", "heap_holder", "stock_ready", "g27_ready")}
h.MODEL_FILE = f"{AGENT}/model2"
h.strata_installed, h.heap_holder, h.stock_ready = (lambda: False), (lambda r: None), (lambda *a: False)
asked = []
h.ask_yn = lambda q, d=False: asked.append(q) or True
open(h.MODEL_FILE, "w").write("qwen36-q4\n")
plan = h.plan_start("strata", {})
assert plan["install"] == "strata" and not plan.get("stock") and not plan.get("weights") and "Qwen3.6-35B-A3B" in asked[0] and "~24 GB" in asked[0], (plan, asked)
ran = []
h.subprocess.run = lambda a, **k: ran.append(os.path.basename(a[1])) or types.SimpleNamespace(returncode=0, stdout="30")
assert h.prepare_start(plan, {}) and ran == ["install-strata.sh"], ran
open(h.MODEL_FILE, "w").write("qwen38-27b\n")
h.g27_ready = lambda m="qwen38-27b": False
plan = h.plan_start("gufo", {})
assert plan["g27"] == "qwen38-27b" and not plan.get("stock") and not plan.get("weights"), plan
ran = []
assert h.prepare_start(plan, {}) and ran == ["download-qwen38-27b.sh"], ran
open(h.MODEL_FILE, "w").write("qwen38-27b-q8\n")
plan = h.plan_start("gufo", {})
# the question says what is still missing (the script's --print-gb), not the fresh-install size
assert plan["g27"] == "qwen38-27b-q8" and "UD-Q8_K_XL" in asked[-2] and "~30 GB of 34" in asked[-2] and plan["g27_gb"] == 30, (plan, asked)
envs = []
h.subprocess.run = lambda a, **k: envs.append(k.get("env", {}).get("GUFO_FAMILY")) or types.SimpleNamespace(returncode=0)
assert h.prepare_start(plan, {}) and envs == ["qwen38-27b-q8"], envs
open(h.MODEL_FILE, "w").write("qwen38-27b\n")
h.subprocess.run = real_run
h.g27_ready = lambda m="qwen38-27b": True
assert not h.plan_start("gufo", {}).get("g27")
assert h.plan_start("strata", {})["stock"] == "UD-Q4_K_XL"  # 27B is gufo's: Strata falls back to Flash-Next
for n, f in saved.items(): setattr(h, n, f)
# Strata launcher by model: the catalog's run script is used whatever the weights answer says, a missing one is named, an unknown model means Flash-Next
open(f"{sd}/run-iq3_s.sh", "w").write("#!/bin/sh\necho iq3\n")
open(f"{sd}/.harness-weights", "w").write("abliterated\n")
sm = lambda m, w, *a: subprocess.run(["bash", strata, *a], capture_output=True, text=True, env=dict(os.environ, STRATA_DIR=sd, STRATA_WEIGHTS=w, STRATA_MODEL=m))
assert sm("flash-next-iq3_s", "stock", "--print-run").stdout.strip().endswith("run-iq3_s.sh") and "iq3" in sm("flash-next-iq3_s", "stock").stdout
r = sm("qwen36-q4", "stock", "--print-run"); assert r.stdout.strip() == "", r
r = sm("qwen36-q4", "stock"); assert r.returncode == 1 and "run-qwen36-ud-q4_k_xl.sh" in r.stderr, r
assert sm("qwen38-27b", "abliterated", "--print-run").stdout.strip().endswith("run-unsloth-ud-q4_k_xl.sh")
for mid, e in h.catalog().items():
    assert ("run" in e) == ("strata" in e) and ("strata" not in e or "strata" in e["engines"]), mid
# gufo launcher: the 27B family needs its own files, and --print-model keeps meaning Flash-Next whatever the saved answer is
gl = os.path.join(os.path.dirname(__file__), "..", "server", "start-gufo-flashnext.sh")
gd = tempfile.mkdtemp(); open(f"{gd}/harness-model", "w").write("qwen38-27b\n")
ge = dict(os.environ, PI_CODING_AGENT_DIR=gd, ENGINE_DIR=f"{gd}/flash", GUFO_27B_DIR=f"{gd}/27b")
assert "/UD-Q4_K_XL/Qwen3.8-Flash-Next" in subprocess.run(["bash", gl, "--print-model"], capture_output=True, text=True, env=ge).stdout
assert subprocess.run(["bash", gl, "--print-model"], capture_output=True, text=True, env=dict(ge, GUFO_FAMILY="qwen38-27b")).stdout.strip() == f"{gd}/27b/Qwen3.8-27B-UD-Q4_K_XL.gguf"
assert subprocess.run(["bash", gl, "--print-model"], capture_output=True, text=True, env=dict(ge, GUFO_FAMILY="qwen38-27b-q8")).stdout.strip() == f"{gd}/27b/Qwen3.8-27B-UD-Q8_K_XL.gguf"
assert h.catalog_id("qwen3.8-27b-q8") == "qwen38-27b-q8" and h.catalog_id("qwen3.8-27b") == "qwen38-27b"  # tune results stay apart
r = subprocess.run(["bash", gl], capture_output=True, text=True, env=ge)
assert r.returncode == 1 and "download-qwen38-27b.sh" in r.stderr, r
assert h.G27_DRAFT in open(gl).read() and h.G27_DRAFT in open(os.path.join(os.path.dirname(gl), "..", "scripts", "download-qwen38-27b.sh")).read()
# live monitor: halogen and Strata have one; Strata uses its own when its venv can run it, else our dashboard; others none
assert h.monitor_cmd("halogen")[1].endswith("halogen-top.sh") and h.monitor_cmd("gufo") is None and h.monitor_cmd("vllm") is None
os.environ["STRATA_DIR"] = md = tempfile.mkdtemp()
for f in ("setup.py", "update.sh", "strata.py"): open(f"{md}/{f}", "w").close()
assert h.monitor_cmd("strata")[2] == "--strata"  # no venv: our dashboard
os.makedirs(f"{md}/.venv/bin"); os.symlink(sys.executable, f"{md}/.venv/bin/python")
sp = h.PROFILES["engines"]["strata"]["port"]  # the mock above moved it
assert h.monitor_cmd("strata", once=True)[1:] == [f"{md}/strata.py", "monitor", "--url", f"http://127.0.0.1:{sp}", "--once"]
assert h.monitor_cmd("strata", "http://nas:8080", True)[4] == "http://nas:8080"
# live reply: the engine answers over HTTP without pi; a dead port and an HTTP error are failures with a reason
assert h.live_reply("llamacpp", "m", timeout=5)[0] is True
dead = h.PROFILES["engines"]["vllm"]["port"]
h.PROFILES["engines"]["vllm"]["port"] = 1  # nothing listens on port 1
ok_, why = h.live_reply("vllm", "m", timeout=2)
assert ok_ is False and "no reply from vllm" in why
h.PROFILES["engines"]["vllm"]["port"] = dead
# `./harness verify` exits by that verdict
rc = subprocess.run([sys.executable, os.path.join(os.path.dirname(__file__), "..", "setup", "harness.py"), "verify", "vllm", "m"],
                    capture_output=True, text=True, env=dict(os.environ, PI_CODING_AGENT_DIR=tempfile.mkdtemp()))
assert rc.returncode == 1 and rc.stdout.startswith("no reply from vllm"), rc
# the wizard's last step trusts the doctor's verdict, and lists what it flagged
real_doc = h.run_doctor
h.run_doctor = lambda: (False, ["\u2717 sidecar not installed"])
assert h.verified("gufo") is False
h.run_doctor = lambda: (True, [])
assert h.verified("gufo") is True
h.run_doctor = real_doc
# halogen preflight: missing docker blocks the plan before any download question; logs live in the cache dir, not /tmp
real_which, real_ask = h.shutil.which, h.ask_yn
h.shutil.which = lambda n, *a, **k: None if n == "docker" else real_which(n, *a, **k)
h.ask_yn = lambda *a, **k: (_ for _ in ()).throw(AssertionError("asked about a download with docker missing"))
assert "docker is not installed" in h.halogen_preflight()[0]
assert h.plan_start("halogen", {}) is None
h.shutil.which, h.ask_yn = real_which, real_ask
# an xrt-only update leaves xrt-plugin-amdxdna with an undefined symbol: caught before the download, not as "No such device" after it
real_run, real_exists = h.subprocess.run, h.os.path.exists
R = lambda rc, out: types.SimpleNamespace(returncode=rc, stdout=out, stderr="")
h.shutil.which = lambda n, *a, **k: "/usr/bin/" + n if n == "docker" else real_which(n, *a, **k)
h.os.path.exists = lambda p: True if str(p).startswith(("/dev/accel", "/dev/kfd", "/usr/lib/libxrt_")) else real_exists(p)
for ldd_out, want in (("\tundefined symbol: _ZN8xrt_core7elf_int18get_partition_sizeE\t(/usr/lib/libxrt_driver_xdna.so.2)\n", True), ("\tlibc.so.6 => /usr/lib/libc.so.6\n", False)):
    h.subprocess.run = lambda cmd, *a, _o=ldd_out, **k: R(0, _o if cmd[0] == "ldd" else "")
    got = h.halogen_preflight()
    assert bool(got) == want and (not want or "does not match the installed xrt" in got[0] and "get_partition_size" in got[0]), got
h.subprocess.run, h.os.path.exists, h.shutil.which = real_run, real_exists, real_which
# the download question comes after the space check: a disk that cannot hold ~135 GB gets an explanation and no question
real_pf, real_ck, real_fg, real_ask2, real_hold = h.halogen_preflight, h.halogen_ckpt, h.free_gb, h.ask_yn, h.heap_holder
h.halogen_preflight, h.halogen_ckpt, h.heap_holder = (lambda: []), (lambda: "/nonexistent/x.hgn"), (lambda running: None)
asked = []
h.ask_yn = lambda q, *a, **k: asked.append(q) or True
h.free_gb = lambda p: 40
assert h.plan_start("halogen", {}) is None and not asked
h.free_gb = lambda p: 500
plan = h.plan_start("halogen", {})
assert plan and plan["download"] is True and asked and "checked against the repo" in asked[0], (plan, asked)
assert h.free_gb("/nonexistent/a/b") > 0  # the nearest existing parent counts
h.halogen_preflight, h.halogen_ckpt, h.free_gb, h.ask_yn, h.heap_holder = real_pf, real_ck, real_fg, real_ask2, real_hold
assert h.engine_log("gufo") == os.path.join(h.LOGDIR, "gufo.log") and h.LOGDIR.endswith("local-ai-harness") and h.LOGDIR != "/tmp/local-ai-harness"
# a box that was never set up is not assumed to be halogen; a pre-wizard halogen install (NPU extension, no profile) still is
real_agent, h.AGENT = h.AGENT, tempfile.mkdtemp()
assert h.saved_engine() is None
try:
    h.cmd_up(None)
    raise AssertionError("cmd_up must refuse an unconfigured box")
except SystemExit as e:
    assert "not set up yet" in str(e)
os.makedirs(f"{h.AGENT}/extensions"); open(f"{h.AGENT}/extensions/npu-retrieval.ts", "w").close()
assert h.saved_engine() == "halogen"
# upgrade pins (scripts/lib-pins.sh): used only while the launcher default is the one they were made on
open(f"{h.AGENT}/harness-pins", "w").write("BASE_GUFO_VERSION=0.32.0\nGUFO_VERSION=0.34.0\n")
assert h.pin_value("GUFO_VERSION", "0.32.0") == "0.34.0" and h.pin_value("GUFO_VERSION", "0.40.0") == "0.40.0"
h.AGENT = real_agent
# declining "Continue?" (or Esc before it) puts every saved answer back; a file the questions created is removed again
real_agent, h.AGENT = h.AGENT, tempfile.mkdtemp()
open(f"{h.AGENT}/harness-sidecar", "w").write("1\n")
snap = h.snapshot_answers()
open(f"{h.AGENT}/harness-sidecar", "w").write("0\n"); open(f"{h.AGENT}/harness-weights", "w").write("abliterated\n")
h.restore_answers(snap)
assert open(f"{h.AGENT}/harness-sidecar").read() == "1\n" and not os.path.exists(f"{h.AGENT}/harness-weights")
h.AGENT = real_agent
# halogen and gufo containers are matched by image name, not port alone (docker is stubbed)
real_run = h.subprocess.run
img = {"a1": "ghcr.io/peonist-ai/halogen-flash-server:0.19.1@sha256:x", "b2": "nginx:latest"}
h.subprocess.run = lambda c, **k: types.SimpleNamespace(returncode=0, stdout="a1\nb2\n" if c[1] == "ps" else img[c[-1]] + "\n")
assert h.engine_containers("halogen") == ["a1"] and h.engine_containers("gufo") == [] and h.engine_containers("llamacpp") == ["a1", "b2"]
h.subprocess.run = real_run
# sidecar default follows the card: off under 24 GB of GPU memory, on above it or when the probe failed; --yes on a small card saves "off"
real_cap = h.capacity
for gb, want in ((12, False), (23.9, False), (24, True), (125.5, True), (0, True)):
    h.capacity = lambda gb=gb: {"fast_gb": gb, "vram_gb": gb, "gpu_name": "", "disk_free_gb": 1}
    assert h.sidecar_default() is want, gb
real_agent, h.AGENT = h.AGENT, tempfile.mkdtemp()
h.set_sidecar(False)
assert h.sidecar_pref() == "0"
h.AGENT = real_agent
h.capacity = real_cap
# a server that will not report its window: keep what models.json holds, else ask, else say the guess
old = {"models": [{"id": "m", "contextWindow": 16384}]}
assert h.known_ctx("ollama", "m", old) == 16384 and h.known_ctx("ollama", "m", None) == 0  # no terminal: build_entry guesses and warns
real_tty, real_input = h.sys.stdin.isatty, __builtins__["input"] if isinstance(__builtins__, dict) else __builtins__.input
h.sys.stdin.isatty = lambda: True
import builtins
for said, want in (("8192", 8192), ("", 0), ("abc", 0), ("512", 0)):
    builtins.input = lambda *_a, said=said: said
    assert h.known_ctx("lmstudio", "m", None) == want, said
builtins.input, h.sys.stdin.isatty = real_input, real_tty
# an engine that will not report its context: the guess is announced, not silent
import io, contextlib
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    assert h.build_entry("ollama", "m", 0, {})["models"][0]["contextWindow"] == 32768
assert "did not report its context window" in buf.getvalue()
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    h.build_entry("ollama", "m", 8192, {})
assert buf.getvalue() == ""
print("ok")

# an engine that drafts sends several tokens per chunk: decode counts tokens (usage), not chunks
class DraftMock(http.server.BaseHTTPRequestHandler):
    """one chunk with 4 tokens of text, then the usage line the server reports for the whole reply"""
    def log_message(self, *a): pass
    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(200); self.end_headers()
        self.wfile.write(b'data: {"choices":[{"delta":{"content":"abcd"}}]}\n\n')
        time.sleep(0.3)  # the window decode is measured over
        self.wfile.write(b'data: {"choices":[],"usage":{"prompt_tokens":900,"completion_tokens":12}}\n\n')
        self.wfile.write(b"data: [DONE]\n\n")


draft = http.server.ThreadingHTTPServer(("127.0.0.1", 0), DraftMock)
threading.Thread(target=draft.serve_forever, daemon=True).start()
ptok, _, tps = h.stream_once(draft.server_port, "osmos-x", "hi", 12)
assert ptok == 900 and 20 < tps < 60, (ptok, tps)  # 12 tokens over ~0.3 s; chunk counting says 1/0.3 = 3.3
print("ok: decode counts tokens, not chunks")

# Strata in Docker always serves with a key (docs/DOCKER.md): start-strata.sh writes it in its data volume, and pi,
# the HTTP probes and the Strata dashboard all have to send it or every request is a 401
kroot = tempfile.mkdtemp()
open(os.path.join(kroot, "harness-key"), "w").write("k" * 32 + "\n")
old_sd = os.environ.get("STRATA_DATA")
os.environ["STRATA_DATA"] = kroot
assert h.strata_key() == "k" * 32 and h.engine_key("strata") == "k" * 32 and h.engine_key("gufo") is None
seen, real_http = {}, h.http

def fake_http(url, body=None, timeout=3, key=None):
    seen["key"] = key
    raise OSError


h.http = fake_http
PRISTINE["served_models"](8080, "strata")
assert seen["key"] == "k" * 32, seen  # status, the ready screen and finish() list models through here
PRISTINE["server_ctx"](h.PROFILES["engines"]["strata"]["port"])  # several engines share :8080; the tests above moved ports around
assert seen["key"] == "k" * 32, seen
h.http = real_http
assert h.build_entry("strata", "qwen36-q4", 262144, {})["apiKey"] == "k" * 32  # pi sends it as the bearer token
assert h.build_entry("gufo", "qwen38-27b-q8", 262144, {})["apiKey"] == "dummy"
os.remove(os.path.join(kroot, "harness-key"))
assert h.strata_key() is None
if old_sd is None:
    os.environ.pop("STRATA_DATA")
else:
    os.environ["STRATA_DATA"] = old_sd
print("ok: Strata's Docker key reaches pi and the probes")
