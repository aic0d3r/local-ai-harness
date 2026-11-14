"""python3 tests/test_entries.py - the shared provider-entry builder: the halogen and sidecar entries equal what setup.sh used to
hard-code, and every install path gives a model the same settings."""
import json, os, sys, tempfile, threading, http.server

AGENT = tempfile.mkdtemp()
os.environ["PI_CODING_AGENT_DIR"] = AGENT
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "setup"))
import adopt, entries

# the literal setup.sh's inline Python carried before the merge (halogen entry); any drift in profiles.json "qwen3" shows up here
QWEN3_COMPAT = {"thinkingFormat": "chat-template",
                "chatTemplateKwargs": {"reasoning_effort": {"$var": "thinking.effort"}, "enable_thinking": {"$var": "thinking.enabled"}}}
LEVELS = {"minimal": None, "low": "low", "medium": "medium", "high": "xhigh", "xhigh": None, "max": None}
SAMPLING = {"temperature": 1.0, "top_p": 0.95, "top_k": 20, "min_p": 0.0}

h = entries.halogen_provider()
assert h["baseUrl"] == "http://127.0.0.1:8731/v1" and h["api"] == "openai-completions" and h["apiKey"] == "dummy"
assert h["models"] == [{"id": "halogen-qwen3.8-flash-next", "name": "halogen Qwen3.8-Flash-Next", "reasoning": True, "contextWindow": 262144,
                        "maxTokens": 8192, "compat": QWEN3_COMPAT, "thinkingLevelMap": LEVELS, "samplingParams": SAMPLING, "input": ["text", "image"]}], h

# the sidecar: what setup.sh ended up with (compat replaced to switch thinking off), minus the image input it wrongly claimed
t = entries.tiny_provider()
assert t["baseUrl"] == "http://127.0.0.1:8090/v1"
assert t["models"] == [{"id": "ling3.0-tiny", "name": "Ling-3.0-tiny sidecar", "reasoning": True, "contextWindow": 131072, "maxTokens": 8192,
                        "compat": {"thinkingFormat": "chat-template", "chatTemplateKwargs": {"enable_thinking": False}},
                        "thinkingLevelMap": LEVELS, "samplingParams": SAMPLING, "input": ["text"]}], t

# a model family gets its settings from profiles.json, an unknown one the default; the engine prefix is not repeated in the name
q = entries.build_provider("http://x/v1", "gufo", "qwen3.8-flash-next", 65536, ["text"])["models"][0]
assert q["reasoning"] and q["compat"] == QWEN3_COMPAT and q["thinkingLevelMap"] == LEVELS and q["maxTokens"] == 8192 and q["name"] == "qwen3.8-flash-next (gufo)"
u = entries.build_provider("http://x/v1", "ollama", "mystery-7b", 8192, ["text"])["models"][0]
assert not u["reasoning"] and "compat" not in u and u["maxTokens"] == 1024  # max(1024, 8192 // 8)
assert entries.build_provider("http://x/v1", "vllm", "vllm-m1", 4096)["models"][0]["name"] == "m1 (vllm)"
assert entries.build_provider("http://x/v1", "vllm", "m1", 4096, name="Mine")["models"][0]["name"] == "Mine"
assert entries.build_provider("http://x/v1", "gufo", "m", 65536, max_tokens=4096)["models"][0]["maxTokens"] == 4096  # a measured value wins

# the flag/Windows installer (adopt.provider) now gives a served model the same entry the wizard would
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        b = json.dumps({"data": [{"id": "qwen3.8-flash-next", "context_length": 65536, "architecture": {"input_modalities": ["text", "image"]}}]}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

srv = http.server.HTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
url = f"http://127.0.0.1:{srv.server_port}"
adopt.provider(url, "gufo")
srv.shutdown()
got = json.load(open(os.path.join(AGENT, "models.json")))["providers"]["gufo"]
assert got == entries.build_provider(url + "/v1", "gufo", "qwen3.8-flash-next", 65536, ["text", "image"]), got
assert got["models"][0]["compat"] == QWEN3_COMPAT  # the settings the old adopt.provider left out
print("ok")
