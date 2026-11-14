"""python3 tests/test_endpoint.py - ./harness endpoint: any OpenAI-compatible server, with a key, on another host or port.
A mock server that enforces a bearer key stands in for it; nothing is installed (install_extensions is stubbed)."""
import http.server, json, os, sys, tempfile, threading, types

AGENT = tempfile.mkdtemp()
os.environ["PI_CODING_AGENT_DIR"] = AGENT
os.environ.pop("HARNESS_ENDPOINT_KEY", None)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "setup"))
import harness as h

KEY = "sekret-123"
REPORTS_CTX = [True]  # does /v1/models say its context window?


class Srv(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def authed(self):
        if self.headers.get("Authorization") == f"Bearer {KEY}":
            return True
        self.send_response(401); self.end_headers()
        return False

    def do_GET(self):
        if not self.authed():
            return
        m = {"id": "gw-model", "architecture": {"input_modalities": ["text", "image"]}, **({"context_length": 65536} if REPORTS_CTX[0] else {})}
        b = json.dumps({"data": [m, {"id": "other"}]}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if not self.authed():
            return
        self.send_response(200); self.end_headers()
        for c in ('{"choices":[{"delta":{"content":"OK"}}]}', "[DONE]"):
            self.wfile.write(f"data: {c}\n\n".encode())


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Srv)
threading.Thread(target=srv.serve_forever, daemon=True).start()
URL = f"http://127.0.0.1:{srv.server_port}"

installed = []
h.install_extensions = lambda eng, *a, **k: installed.append(eng) or True  # the real one runs setup.sh
ns = lambda url, **kw: types.SimpleNamespace(url=url, **{"name": None, "key": None, "model": None, "ctx": None, **kw})
rj = lambda f: json.load(open(os.path.join(AGENT, f)))


def fails(**kw):
    try:
        h.cmd_endpoint(ns(**kw))
    except SystemExit as e:
        return str(e.code)
    raise AssertionError("expected the command to refuse")


# a server that wants a key and is not given one: refused, with the way out, and nothing written
msg = fails(url=URL)
assert "HTTP 401" in msg and "--key" in msg and "HARNESS_ENDPOINT_KEY" in msg, msg
assert not os.path.exists(f"{AGENT}/models.json") and not os.path.exists(f"{AGENT}/harness-profile.json")

# bad names / bad URLs / names that belong to a local engine
assert "not a URL" in fails(url="localhost:8080")
assert "own engines" in fails(url=URL, key=KEY, name="strata")
assert "lower-case" in fails(url=URL, key=KEY, name="Bad Name")

# the happy path: key from the environment, model + context from the server (/v1 on the URL is accepted)
os.environ["HARNESS_ENDPOINT_KEY"] = KEY
h.cmd_endpoint(ns(url=URL + "/v1/", name="gw"))
os.environ.pop("HARNESS_ENDPOINT_KEY")
m = rj("models.json")["providers"]
p = m["gw"]
e = p["models"][0]
assert p["baseUrl"] == URL + "/v1" and p["apiKey"] == KEY and p["api"] == "openai-completions", p
assert e["id"] == "gw-model" and e["contextWindow"] == 65536 and e["input"] == ["text", "image"] and e["name"] == "gw-model (gw)", e
assert "llamacpp-tiny" in m  # the sidecar entry pi's compaction looks up
s = rj("settings.json")
assert s["defaultProvider"] == "gw" and s["defaultModel"] == "gw-model" and s["compaction"]["reserveTokens"] == h.reserve_for(65536), s
prof = rj("harness-profile.json")
assert prof["engine"] == "custom" and prof["provider"] == "gw" and prof["url"] == URL and prof["model"] == "gw-model", prof
assert installed == ["custom"]  # the pi extensions are installed for it (setup.sh --no-halogen path)
assert h.saved_engine() == "custom" and h.custom_endpoint() == {"url": URL, "provider": "gw", "key": KEY}

# verify / status / report / tune / up for it
assert h.live_reply("custom", "gw-model", timeout=5)[0] is True
import io, contextlib
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    h.cmd_status(None)
assert "engine custom" in buf.getvalue() and "UP" in buf.getvalue() and "gw-model" in buf.getvalue(), buf.getvalue()
rep = h.report_text(doctor=False)
assert "custom endpoint, http://<host>, up" in rep and "127.0.0.1" not in rep and str(srv.server_port) not in rep and KEY not in rep, rep
try:
    h.cmd_tune(None); raise AssertionError("tune must refuse")
except SystemExit as ex:
    assert "not measured" in str(ex.code)
assert h.engine_update("custom") == "" and h.engine_have("custom") == ""  # no profile entry: quiet, not a KeyError
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    h.cmd_up(None)  # nothing to start; refreshes the install
assert installed == ["custom", "custom"] and "not answering" not in buf.getvalue(), (installed, buf.getvalue())

# a wrong key is named as such, not as a dead server
mj = rj("models.json"); mj["providers"]["gw"]["apiKey"] = "wrong"; json.dump(mj, open(f"{AGENT}/models.json", "w"))
ok, why = h.live_reply("custom", "gw-model", timeout=5)
assert ok is False and "HTTP 401" in why and "wrong or missing key" in why, why
assert h.served_models(0, base=URL, key="wrong") == [] and h.served_models(0, base=URL, key=KEY) == ["gw-model", "other"]

# a server that does not report its context window: --ctx wins; without it 32768 with a warning; --model picks another model
REPORTS_CTX[0] = False
h.cmd_endpoint(ns(url=URL, name="gw", key=KEY, ctx=131072, model="other"))
e = rj("models.json")["providers"]["gw"]["models"][0]
assert e["id"] == "other" and e["contextWindow"] == 131072, e
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    h.cmd_endpoint(ns(url=URL, name="gw", key=KEY))
assert rj("models.json")["providers"]["gw"]["models"][0]["contextWindow"] == 32768 and "assuming 32768" in buf.getvalue()

# an unreachable server is refused unless the model and window are both given (then it is configured and the live check fails)
dead = "http://127.0.0.1:9"
assert "did not answer" in fails(url=dead, key=KEY)
try:
    h.cmd_endpoint(ns(url=dead, name="off", key=KEY, model="m", ctx=8192)); raise AssertionError("the live check must fail")
except SystemExit as ex:
    assert ex.code == 1
assert rj("models.json")["providers"]["off"]["models"][0]["contextWindow"] == 8192
print("ok")
