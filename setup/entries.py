"""The one place that builds the provider entries written into pi's models.json. The wizard (harness.py), the flag/Windows
installer (adopt.py) and setup.sh's halogen path all go through here, so a model gets the same reasoning, chat-template,
sampling and thinking-level settings whichever way it was installed. Per-model settings come from config/profiles.json
("models"); nothing about a model family is written twice. Stdlib only (runs on Windows too).
"""
import json, os, re

_PROFILES = None


def load_profiles():
    global _PROFILES
    if _PROFILES is None:
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "..", "config", "profiles.json"), encoding="utf-8") as f:
            _PROFILES = json.load(f)
    return _PROFILES


def model_profile(model_id, profiles=None):
    """Settings for a served model id: profiles.json "models" default, overlaid by the first family whose regex matches."""
    m = (profiles or load_profiles())["models"]
    out = json.loads(json.dumps(m["default"]))
    for k, v in m.items():
        if k != "default" and re.search(v["match"], model_id, re.I):
            out.update({a: b for a, b in v.items() if a != "match"})
            break
    return out


def build_provider(base_url, engine, model_id, ctx, inputs=("text",), profiles=None, max_tokens=None, name=None):
    """-> {baseUrl, api, apiKey, models: [entry]} for one served model. max_tokens: a measured value (./harness tune) beats the
    default of a fraction of the window. name: overrides the profile's or the generated display name."""
    mp = model_profile(model_id, profiles)
    max_tokens = max_tokens or min(mp["maxTokens"], max(1024, ctx // 8))
    # servers that prefix their ids with the engine (osmos-osmos-evo-a) need not repeat it in the name
    shown = model_id[len(engine) + 1:] if model_id.lower().startswith(engine.lower() + "-") else model_id
    entry = {"id": model_id, "name": name or mp.get("name") or f"{shown} ({engine})", "reasoning": mp["reasoning"],
             "input": list(inputs), "contextWindow": ctx, "maxTokens": max_tokens, "samplingParams": mp["samplingParams"]}
    for k in ("compat", "thinkingLevelMap"):
        if k in mp:
            entry[k] = mp[k]
    return {"baseUrl": base_url, "api": "openai-completions", "apiKey": "dummy", "models": [entry]}  # set the key by hand if the engine runs with one


HALOGEN_MODEL = "halogen-qwen3.8-flash-next"
TINY_MODEL = "ling3.0-tiny"


def halogen_provider(profiles=None):
    """halogen on :8731 with its vision tower on (server/start-halogen.sh sets HALOGEN_VISION_TOWER=1), at its native 262144 window."""
    return build_provider("http://127.0.0.1:8731/v1", "halogen", HALOGEN_MODEL, 262144, ("text", "image"), profiles, name="halogen Qwen3.8-Flash-Next")


def tiny_provider(profiles=None):
    """The ling-tiny sidecar on :8090, so compaction and branch summaries find it in the model registry. Text only. It thinks by
    default, which ate the budget and 8.6x'd latency (measured 10-06), so thinking is switched off at the template level."""
    q = (profiles or load_profiles())["models"]["qwen3"]
    p = build_provider("http://127.0.0.1:8090/v1", "llamacpp-tiny", TINY_MODEL, 131072, ("text",), profiles, name="Ling-3.0-tiny sidecar")
    e = p["models"][0]
    e.update({"reasoning": True, "maxTokens": 8192, "samplingParams": q["samplingParams"], "thinkingLevelMap": q["thinkingLevelMap"],
              "compat": {"thinkingFormat": "chat-template", "chatTemplateKwargs": {"enable_thinking": False}}})
    return p
