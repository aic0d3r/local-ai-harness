# Operations: 262k Context, Memory Fragmentation and Troubleshooting

This page covers the halogen path (Strix Halo). On other hardware or with cloud models, see
[Engines](ENGINES.md); most of it does not apply.

## Context size and memory fragmentation

The default launch is halogen's native max: **262,144 ctx x 4 slots**. Memory is not the
constraint (~67 GB model + ~115 MiB/slot in-place cache) — allocator fragmentation is.

The launcher's pre-flight gate handles it: if order-9 pages are low (box up a long time,
big-model churn) it first attempts recovery (page-cache drop, compaction) and only proceeds on a
healthy allocator; otherwise it fails in seconds with the exact fix command instead of wedging
for 30+ minutes. Install the `vm-compact.timer` (`setup.sh`/`--doctor` print the one-line
command) to keep the allocator healthy day to day.

```sh
HALOGEN_CTX=65536 HALOGEN_KV_SLOTS=2 ./start-halogen.sh   # small-context opt-out
```

After any ctx change the launcher syncs `models.json`'s `contextWindow` (so pi token-budgets
against the real window) and scales `compaction.reserveTokens` (60k at 262k, 10k at 65k).

## Watching speed (prefill / decode)

| Engine | Live, while the agent works | A number to compare |
| --- | --- | --- |
| halogen | `./harness monitor` (halogen-top.sh) and the `halogen` footer in pi | `./harness tune` (1 min) / `./harness bench` (`bench-v1`) |
| Strata | `./harness monitor` (`strata monitor`, else halogen-top.sh `--strata`) | same |
| gufo, osmos, llama.cpp, Ollama, LM Studio, vLLM, custom | the `speed` footer in pi and `./harness monitor` (per-reply log) | same (`tune`/`bench` skip a custom endpoint) |

On engines without a usable `/metrics` (gufo has none), pi times each reply itself: prefill = prompt
tokens the server did not have cached / time to first streamed token, decode = output tokens / the rest. One line per
reply goes to `~/.pi/agent/harness-speed.jsonl`, which `./harness monitor` follows for the active engine, each rated reply in the
footer's format (`--once`: the last 10). The time to the
first token includes queueing and the network, so prefill reads a little low. While subagents run at the same time, a reply's rate
is its share, not the engine's total. Drafted decode depends on the text (code drafts better than prose), and an engine that
does not report cached tokens counts the whole prompt as new. Replies with under 256 new prompt tokens or 16 output tokens get no rate, and neither do replies that arrived in one burst
(osmos sends a whole tool call in one chunk, so where prefill ended is unknowable from the client). Checked against osmos's own
`timings` through a logging proxy (2026-11-22, pi RPC mode, 4 replies): prefill 89-94 vs 91-96 t/s, decode 23.1-25.8 vs 22.7-25.3 t/s.

The footer shows the size and the wait beside the rate (`pf 94/s (566 tok, 6.8s)`) because short prefills read far below
`./harness tune`'s 8K figure. That is the engine, not the measurement. On osmos-evo the experts that do not fit the memory budget
stream from the SSD, and a few hundred tokens already touch most of them. Measured here (2026-11-22, cold prompts, `/sys/block/nvme0n1/stat`):
50 tokens 2.1 s / 5.4 GB read, 562 tokens 3.5 s / 12.3 GB, 2,135 tokens 7.0 s / 17.6 GB, 4,580 tokens 12.5 s / 28.1 GB (prefill works
in 4,096-token chunks, ~7 s each). So an agent turn that adds 500 tokens pays most of a full chunk: ~130 t/s at 566 tokens,
~300 at 2K, ~440 at 8K, ~500 at 21K.

Halogen's own surfaces, no built-in web UI in the image (`/`, `/gui`, `/dashboard` are 404):

- **per request** — every response (and the last streamed chunk) carries a `timings` object:
  `prompt_per_second`, `predicted_per_second`, `cache_n`, `draft_n_accepted`.
- **per session** — `serve_api:` lines in the container log, one per request.
- **aggregate** — `GET /metrics` (Prometheus text, llama.cpp-compatible counter names) and
  `GET /cache` (prompt-cache hit rate, evictions, KV-pool usage). The two `*tokens_seconds`
  gauges mean "since the last scrape", so hand-curling them is misleading.

Two tools ship for it:

```sh
./harness monitor                    # the right monitor for the active engine (--once for one snapshot; strata --url http://host:8080)
                                     # Strata: its own `strata monitor` (Textual) when its venv has Textual, else halogen-top.sh --strata
./scripts/halogen-top.sh              # live dashboard: busy state, KV, per-request t/s, cache; piped = log lines
./scripts/halogen-top.sh --strata     # same against Strata (:8080 or --strata <url>); no cache panel
./scripts/session-audit.sh --perf     # prefill/decode for one session's own time window
./scripts/session-audit.sh --harness  # sidecar/NPU calls, latency, tokens offloaded, fallbacks
```

Point `halogen-top.sh` at a remote box with `HALOGEN_URL=http://host:8731`. It appends each scrape to `$XDG_STATE_HOME/halogen-top/history.csv`; that file is
what `--perf` uses to diff `/metrics` over a window — the counters are cumulative and carry no session
id, so a window that was never recorded can only be rebuilt from the logs. Leave it running (e.g. in
a `screen` session) for the diff to work. Grafana/Prometheus works too (scrape `127.0.0.1:8731/metrics`) —
the dashboards are yours to build; these two scripts exist because they cost no extra containers.

## Troubleshooting

| symptom | fix |
|---|---|
| any `--doctor` line red | the line prints the exact fix |
| halogen won't launch, order-9 < 200 | reboot (memory fragmentation); the launcher's 65k/2-slot fallback works without one |
| halogen tries to download something | your launch has `HALOGEN_DOWNLOAD` set — remove it; everything loads from local files |
| sessions 2.5x slower | prompt cache is OFF — never set `HALOGEN_PROMPT_CACHE=0` |
| `triage` / `dedup_scan` return an error | halogen down, or the model missing from `HALOGEN_NPU_MODELS` — check the `npu` field of `curl -s localhost:8731/health` |
| hangs or garbage when GPU and NPU work at once | GPU fabric clock not held — `./setup.sh --doctor` prints the one-time unit install |
| semantic search unavailable | `uvx` missing (install uv) or `mcp.json` lacks the semble entry — rerun `./setup.sh` and check `pi mcp list` |
| web search unavailable | `uvx` missing or `mcp.json` lacks the ddg entry — rerun `./setup.sh`; DDG rate-limits searches to 30/min by default |
| stale task on resume | checkpoint from a previous task — run `/progress-reset` in pi (checkpoints live in `~/.pi/agent/progress/`, one file per project folder) |
| want zero-touch sidecar after reboot | `systemctl --user enable ling-tiny` (off by default on purpose) |

## Stopping the servers

`scripts/stop-all.sh` stops halogen, gufo, Strata, osmos and the ling-tiny sidecar, then pops a desktop notification (it warns if GPU memory stays held, which needs a reboot). `scripts/stop-all.sh --install` adds "Stop LLM servers" and "Start LLM servers" entries to the app menu and ~/Desktop. The start one (`scripts/start-servers.sh`) runs `./harness up` headless (the first step of `start.sh`: starts the engine you picked in setup, refreshes the install), with no terminal and no pi, and notifies when the engine answers; then run `pi`. Without a terminal it asks nothing, so an engine holding the GPU memory is left running and the notification says so. pi and engines the harness does not start (Ollama, vLLM, ...) are untouched.
