# Facts for replies (Grok project file)

Rules: quote numbers from this file first. If it isn't here, look in docs/BENCHMARKS.md, docs/ENGINES.md, docs/LLAMACPP.md,
docs/STRIX-HALO.md, then CHANGELOG.md; this file wins when they disagree (it is newer). If none of them has it, say
"haven't tested that". Lowercase, short, no hype.
Rig for every number: ASUS ROG Flow Z13, Ryzen AI Max+ 395 (Strix Halo, gfx1151), Radeon 8060S, 128 GB unified, Linux, 70 W.
Harness: https://github.com/aic0d3r/local-ai-harness (detects, installs, switches and benchmarks engines; tunes the pi agent per engine).
"Decode" = output tokens/s. "Short prompt" numbers are not long-context numbers; always give the deep-context figure too.

## Qwen3.8 Flash-Next 125B (default model)

| engine | decode | prefill | context | source |
|---|---|---|---|---|
| halogen (closed, NPU+GPU) | 70.4 t/s stock, 71.6 abliterated, short prompt | ~1,760-1,840 t/s | 262k x 4 slots | ./harness tune, bench-v1, 2026-11-27 |
| halogen | 47-50 t/s at 2k-119k context | ~1,520 t/s cold (131k in 86 s) | 256k | BENCHMARKS.md, halogen 0.18.5 |
| Strata (open) | 43 t/s battery, 32-36 at depth | ~990 t/s | 256k | BENCHMARKS.md, Strata 0.1.44.1 |
| gufo (MIT) | 40 t/s battery, 29-32 at depth | ~1,270 t/s | 256k | BENCHMARKS.md, gufo 0.12.0 |
| llama.cpp | not measured | | | LLAMACPP.md |

halogen is the only engine with NPU tools (guard, triage, dedup).

## Qwen3.6-35B-A3B

- strix-llama.cpp (halo-box image, Vulkan) + MTP: 84-85 t/s decode, ~2,000-2,100 t/s prefill, 85-88% drafts accepted (bench-v1, 2026-11-17/25).
- Same engine without MTP: ~60 t/s, ~1,000 t/s prefill.
- Faster than Flash-Next; Flash-Next is the quality upgrade, not a speed upgrade.

## Inkling 2 Mini (26B looped MoE)

| engine | decode | prefill | context | notes |
|---|---|---|---|---|
| osmos (release NVFP4/MXFP8 pack, 19.6 GB) | 47-48 t/s | ~1,040-1,210 t/s | 1M | bench-v1, 2026-11-23/27 |
| Strata 0.2.22 image, SQ-20 | 32-39 t/s | ~860-1,400 t/s | 128k | no thinking in Strata's build; tool calls work |
| strix-llama.cpp (Vulkan, IQ4_NL) | 20 t/s | 324 t/s | | graph split 194 ways; ROCm image kept ~16 CPU cores busy |

- Use temperature 1.0 (the release's setting): greedy looped on one tool call in long agent sessions.
- Speed depends on the engine, not the agent (pi, Hermes, etc. should see about the same).
- Strata 0.2.23 (2026-11-27) adds DFlash 2 and claims faster Strix Halo prefill (4K: 887 -> 1,528 t/s); not tested here yet. Its ROCm Docker image was not published at release time (only 0.2.23-cuda13).

## Other models

- Qwen3.8-27B on gufo: ~36-41 t/s decode, ~400 t/s prefill, 262k.
- DeepSeek-V4.1-Flash (~768B) on osmos, streaming experts from SSD: ~35-37 t/s decode, ~490 t/s prefill, 1M context (bench-v1, 2026-11-23/27).

## Don't claim

- Any llama.cpp vs halogen gap on Flash-Next (unmeasured).
- 74.1 t/s (older chart number, not in the current bench log).
- Numbers for hardware other than the rig above.
