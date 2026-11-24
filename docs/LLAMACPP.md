# strix-llama.cpp (Vulkan) Setup for Qwen3.8 on Strix Halo

The alternative engine for Strix Halo (menus call it strix-llama.cpp, engine id `llamacpp`; on other hardware run your
own upstream llama-server and the harness detects it as plain llama.cpp): [halo-box/strix-llama.cpp](https://github.com/halo-box/strix-llama.cpp), the
Strix Halo Vulkan/ROCm fork, instead of the halogen container.
Full control over flags, drafter and GGUF quant, **256k ctx** max, and the way to go for the
**27B** runs. You lose every NPU feature — see [Engines](ENGINES.md).

Use `./setup.sh --no-halogen` for the pi wiring on this path, or wire it by hand as below.

## 1. Engine

[halo-box/strix-llama.cpp](https://github.com/halo-box/strix-llama.cpp) publishes both routes: release zips
(`llama-b<build>-ubuntu-vulkan-x64.zip`, `-ubuntu-rocm-gfx1151-x64.zip`, ROCm 10.1 bundled) and container images --
`ghcr.io/halo-box/strix-llama.cpp:server-vulkan` (1.3 GB, Mesa RADV bundled, entrypoint `/app/llama-server`) and
`:server-rocm`. `server/start-qwen36.sh` runs the Vulkan image (pinned by digest: `server-vulkan-b13600`, Halobox v1.1.0 +
11 commits) for Qwen3.6-35B-A3B, so nothing is built here. Release zips: v1.1.0 is build `b1069`
(`llama-b1069-ubuntu-vulkan-x64.zip` / `-ubuntu-rocm-gfx1151-x64.zip`); they keep the execute bit now, no `chmod +x`.

```bash
./harness use llamacpp        # starts server/start-qwen36.sh on :8080 and wires pi
```

Measured on this box (2026-11-16, `bench-v1`, 131072 ctx, one engine at a time): **85.3 t/s decode and 2,048 t/s
prefill with the model's own MTP head** (`--spec-type draft-mtp --spec-draft-n-max 2`, 85-88% of drafts accepted),
59.8 / 1,017 for the same model without the draft, 51.7 / 601 on the Strata image, and gufo cannot load the
architecture at all. `docs/BENCHMARKS.md` has the full table.

The v1.1.0 image (2026-11-23, `bench-v1`, 3 runs, 8K depth, back to back with the old `27ffff9f`): 83.1 t/s code / 78.5
prose decode, 2,155 t/s prefill, against 82.9 / 76.1 / 2,169 on the old image - the same speed. The Ling-3.0-tiny sidecar
also held (a ~51.6K-token summarize: 1,353 t/s prefill, 106 decode vs 1,325 / 104). Most of v1.1.0's Vulkan speed work
(#252-#289) was already in the old image; what is new for us is the Flash-Next + MTP long-context load fix (#304),
slot sampler reuse (#274) and the upstream sync.

**ROCm** (`LLAMACPP_BACKEND=rocm`, the same build's `server-rocm-b13600` image): slower here. Qwen3.6 decoded 70.0 t/s
(83.1 on Vulkan) at the same 2,136 t/s prefill and used twice the CPU. Kept as an opt-in for comparison, not a default.

**Flash-Next** on this image drafts with its MTP head (`mtp-Qwen3.8-Flash-Next-shared-Q8_0.gguf`, the one
`scripts/download-stock.sh` puts beside the shards) and runs halo-box's recipe `-lzm on-direct`, 3 drafts.
`FLASHNEXT_MTP=0` turns both off. Not measured here yet; watch memory on the first load (halo-box issue #206: shared
MTP head + `-lzm on-direct` used much more memory on some builds).

**Inkling 2 Mini is not served here**: on this image it decoded 20 t/s and prefilled 324 t/s (IQ4_NL, Vulkan), its graph
split 194 ways, and the ROCm image kept ~16 CPU cores busy. It runs on osmos instead ([Engines](ENGINES.md#osmos-deepseek-v41-flash-from-the-ssd-1m-context-strix-halo)).

Build from source if you want to change the build itself (halo-box's zips and the image above ship their own Mesa; the
container measured faster than the local `qwen4exp` build did here).

## 2. Models

- **27B target** — [unsloth/Qwen3.8-27B-GGUF](https://huggingface.co/unsloth/Qwen3.8-27B-GGUF)
  UD-Q4_K_XL (Dynamic v3, the pick; the Q4-Q8 PPL plateau is flat). For exact reproduction of
  published-era numbers: UD-Q5_K_XL @ revision `408fcc1807ab` (v2), set `TARGET_GGUF`.
- **27B vision** — `mmproj-F16.gguf` (same repo), passed as `-mm ... --mmproj-offload` so pi
  sessions always have the projector on GPU. A/B (2026-09-07): projector GPU vs CPU placement
  is byte-identical on decode — vision is free.
- **pi image input** — the model entry in `models.json` must declare
  `"input": ["text", "image"]` or pi refuses to send images.
- **27B drafter** — [incoai/Qwen3.8-27B-DFlash2-GGUF](https://huggingface.co/incoai/Qwen3.8-27B-DFlash2-GGUF)
  Q4_K_M · **Flash-Next drafter** —
  [EasiiX MTP Q8_0](https://huggingface.co/EasiiX/Qwen3.8-Flash-Next-MTP-Strix-Halo-GGUF)
- **Flash-Next (llama.cpp variant)** —
  [unsloth UD-IQ4_XS](https://huggingface.co/unsloth/Qwen3.8-Flash-Next-GGUF) (3 shards) +
  `mmproj-F16.gguf`
- **Aux sidecar** — [inclusionAI/Ling-3.0-tiny](https://huggingface.co/inclusionAI/Ling-3.0-tiny)
  Q4_K_M (runs happily on any halo-box release zip or the container image). This is the server every ling-tiny
  extension talks to on `:8090`; it is a GPU model, not an NPU one.
- **Template (27B)** — vendored in `config/template/` (`sharp-v22.3.2.jinja`). Upstream:
  [peculiar-ragdoll/Qwen-Sharp-Chat-Templates](https://huggingface.co/peculiar-ragdoll/Qwen-Sharp-Chat-Templates)

## 3. Servers

One big model at a time on this APU; a second 27B-class server means device-lost.

```sh
ENGINE_DIR=/path/to/halo-box/build/bin MODEL_DIR=... server/start-flashnext.sh   # daily driver
ENGINE_DIR=/path/to/halo-box/build/bin MODEL_DIR=... server/start-qwen38.sh      # 256k / low-memory
server/start-ling-tiny.sh   # sidecar (second terminal): pinned strix-llama.cpp image; ENGINE_DIR=... runs a native build
```

**GPU memory** — the ~91 GB model lives in the GTT heap, so size it: boot args
`amdgpu.gttsize=126976 ttm.pages_limit=32505856 ttm.page_pool_size=32505856` (on systemd-boot
put them on the single `LINUX_OPTIONS` line — a stray newline there makes the loader silently
drop the whole line). **Never force `--fit off`**: full offload onto an undersized heap
deadlocks the iGPU and hard-locks the host.

## 4. pi wiring

```sh
cp config/models.json.example ~/.pi/agent/models.json   # adjust paths/ports
cat config/settings-snippet.json                        # merge into settings.json
cp -r extensions/* ~/.pi/agent/extensions/              # dirs too: the .ts files import harness-profile/
```

Two numbers in the snippet you must understand, not copy: `reserveTokens` ships **60000**
for the 262144 window both launchers serve (what the halogen launcher sets at 262k); on a smaller `CTX` use a quarter of it
(16384 for 65536). `./harness up` and the halogen launcher resync it to the served window on their own.
`thinkingBudgets` (per-request budgets pi sends) are what actually govern
pi sessions; the server's `--reasoning-budget` flag is only a default for clients that do not
send one. On this path the NPU extensions are inert — there is no halogen to serve them.

## 5. Verify you are actually local

`curl localhost:8080/metrics` while pi runs; if `prompt_tokens_total` is not climbing, pi fell
back to a cloud provider.
