# Setup and daily use

The full walkthrough of `./setup.sh`, the `./harness` commands and `./start.sh`. Back to the [README](../README.md).

## Guided setup

`./setup.sh` in a terminal (same as `./harness setup`) runs a wizard that works for any engine:

0. **Checks for harness updates.** If a newer release (`vX.Y.Z` tag) is out it lists the new commits, offers
   a fast-forward to that tag, then restarts on the new code. Unreleased master commits are not offered. Local edits
   that block the update are named in one line; it stays quiet offline or when there is nothing new.
1. **Detects** your hardware and which engines are already serving: halogen, gufo, strix-llama.cpp, Strata, osmos,
   a plain llama.cpp llama-server, vLLM, Ollama, LM Studio.
2. **Asks which model, then which engine, every run.** On Strix Halo the first menu is the model (each on its fastest
   engine here, the recommended one first; "Pick an engine instead" opens the engine menu). Esc in any menu after the
   first goes back one menu; Esc in the first one cancels, and Ctrl-C always does. The engine menu (current engine
   first) shows the installed version, the measured decode speed of the model each would run, which one is current, running
   (and its model) or recommended, what you lose off halogen (the NPU tools) and which running engine a start would stop.
   llama.cpp, LM Studio, Ollama and vLLM get their own row only while they answer; otherwise one "Another server on this
   box" row lists them. Picking an engine that is down offers to start it (halogen, gufo, Strata, osmos, CIRU, strix-llama.cpp), asks before
   stopping the one holding the memory, and waits until it answers. On a fresh box with no engine running the menu still
   shows, halogen first on an NPU box, and picking halogen without a checkpoint offers the ~124 GB download
   ([`scripts/download-halogen.sh`](../scripts/download-halogen.sh)). Other engines print how to start them. A start you decline (or one this
   box cannot do) brings the first menu back instead of ending setup. To switch later, run `./setup.sh` again or
   `./harness config`.

   **All questions come first**, then a summary (your choices, then the numbered steps it is about to run) and one `Continue? [Y/n]`; after that the run needs
   nothing from you. Before `Continue?` nothing has been installed or started, and **Esc** (or Ctrl-C) at any prompt
   exits cleanly. Prompts use `gum` when it is installed. The first run prints the full install output; later runs print
   one line per step with only warnings and failures in full (everything goes to `~/.pi/agent/setup.log`; `./harness setup --verbose`
   shows it all). One-time sudo fixes (fabric clock, vm-compact) are shown once, then only by `--doctor` until fixed.
3. **Writes the pi provider** with per-model defaults (reasoning, sampling, compaction reserve) from
   [`config/profiles.json`](../config/profiles.json), and records the active engine in a profile that the sidecar, guard
   and NPU extensions read, so they follow your engine instead of assuming halogen.
4. **Probes** prefill and decode for ~1 min and sizes `maxTokens` and the compaction reserve to what it measured, then
   prints a short summary and offers to start pi in the current directory. The probe is one short prompt (a few thousand
   tokens), so its speeds are for sizing limits only. They are not comparable to the engine benchmark tables in
   [BENCHMARKS.md](BENCHMARKS.md#engines-on-strix-halo), which use the `halogen-cmp.py` battery and 2k-119k depth prompts.
5. **Extras, remembered between runs** (`./harness config` changes one; `./harness setup --reconfigure` asks all
   again):
   - whether to run the **ling-tiny sidecar**, asked on the first wizard run, then shown in the summary and changed in `./harness config` (`--reconfigure` asks again) (default yes where the GPU has 24 GB or more, no on smaller cards; it holds ~8 GiB of GPU memory, and "no" means the main
     model writes commits and summaries, with compaction blocking on it; non-interactive: `./setup.sh --no-sidecar` /
     `--sidecar`);
   - the engine update check (any engine with a known release source: halogen, gufo, Strata, osmos, CIRU, plain llama.cpp, vLLM,
     Ollama; strix-llama.cpp runs an image pinned by digest, so it has none).
     On halogen, gufo, Strata and osmos it offers the safe upgrade right away (`scripts/upgrade-<engine>.sh`: update, restart,
     check, roll back on failure); on the others it prints the upgrade command. If an upgrade passes its checks but runs worse, the
     `rollback` row in `./harness config` puts it back on the version from before it (Strata: Docker only; osmos rebuilds the previous tag and checks it), or keeps the new one and deletes the older versions. A passing upgrade keeps only the new version and the rollback;
     `./harness prune` deletes older engine versions already on disk (lists them, asks once). `start.sh` repeats the one-line notice,
     and `./harness update-check` runs it on demand;
   - voice.

   Serena language servers for the languages found in your project are enabled automatically in `.serena/project.yml`
   (restart serena to load them).

Servers you started yourself are never restarted. Run non-interactively (scripts, CI) and `./setup.sh` does the plain
install instead: on a box the wizard already set up for another engine it refreshes for that engine (`./harness refresh`),
and it never downloads the sidecar model without a saved answer or a terminal.

### Flags

- `--halogen auto` finds `*.hgn` in `.`, `/models`, `~/models` and `~/Downloads` (or pass a path). Restart later with
  `./start-halogen.sh`, which is written on first launch.
- `--no-halogen` installs pi without container management: a laptop pi against your own providers, or against a remote
  Strix Halo appliance via `baseUrl` + `PI_NPU_BASE`.
- `--strata [url]` and `--gufo [url]` do a pi-only install with a provider for that engine.
- `--endpoint URL [--name N] [--key K] [--model ID] [--ctx N]` uses any OpenAI-compatible server (another machine, a hosted gateway) as the main engine: no launcher, the key stays in `models.json` ([docs/ENGINES.md](ENGINES.md#any-openai-compatible-server-a-custom-endpoint)).
- `--no-pi` (also asked by the wizard) skips all pi wiring: engine and ling-tiny sidecar only, then prints the
  OpenAI-compatible endpoints for opencode or any other agent. Independent of `--no-halogen`. The pi extensions (NPU
  tools, guards, turn-timer, MCP exposure, skills) are pi-only and are not installed. `./setup.sh --pi` undoes it.
- Also: `--voice`, `--halogen-upgrade [tag]`, `--uninstall`, `--help`.

Prerequisites for halogen: docker (setup will not sudo-install it) and, for the NPU driver, an IOMMU-enabled gfx1151 box.
The launch is pinned to local files (no `HALOGEN_DOWNLOAD`), so the engine has nothing to fetch. The image is closed and
is not network-sandboxed, so this is configuration, not a guarantee. Plain `pi`
also works after setup: startup defaults point at the engine you chose, and existing overrides are never touched.

### Commands

| command | does |
|---|---|
| `./harness setup` | the wizard above |
| `./harness detect` | list hardware and running engines, change nothing |
| `./harness recommend` | pick an engine and model for the detected hardware |
| `./harness use <engine>[/<model>]` | switch pi to another engine or model; in a terminal, if that engine is down (halogen, gufo, Strata, osmos, CIRU, strix-llama.cpp) it asks to stop the engine holding the GPU memory, download what is missing, and starts it. Without a terminal it only switches to what is already up |
| `./harness status` | engine up/down, model, NPU, saved tuning, sidecar state |
| `./harness config` | menu of saved answers (engine, model, weights, agent, sidecar, voice, update check, sudo hints) plus a `running` row that stops engines; change one, applied right away |
| `./harness tune` | re-run the speed probe and re-size the limits |
| `./harness monitor` | live speed for the active engine: halogen and Strata dashboards, on any other engine the prefill/decode of each pi reply (see [Operations](OPS.md#watching-speed-prefill--decode)) |
| `./harness bench` | fixed-protocol speed benchmark (`bench-v1`, 2K/8K/32K), saved to `harness-bench.json`; compare only with other `bench` runs |
| `./harness verify` | ask the active engine for a live reply over HTTP; exit 0 only if it answered (the doctor's ready check) |
| `./harness report` | what a bug report needs, as plain text to paste into an issue: versions, hardware, engine, model, context, measured speed, the doctor's failures. No prompts, keys or paths; home folder and user name redacted (`--no-doctor` skips the doctor run) |
| `./setup.sh --doctor` | read-only health check of the whole install; exits 1 if any check fails (`--refusals` also checks the running model for over-refusal on 19 one-line tasks) |
| `./setup.sh --refresh-mcp` | reset the serena/semble MCP exposure and ensure the default DDG server (backs up `mcp.json` first) |

## Daily use

```sh
# daily driver, any engine: heals what it manages, then opens the pi TUI on the engine ./setup.sh picked
./start.sh ~/my-project
```

`start.sh` (= `./harness up`, then pi) does five things:

1. Starts the engine you picked if it is down (halogen, gufo, Strata, osmos, CIRU, strix-llama.cpp). It asks first if another
   engine holds the GPU memory, and never launches one engine beside another that holds it.
2. Re-checks the install quietly. Only problems print; the full log is `~/.pi/agent/setup.log`.
3. Shows the update notice with the upgrade offer.
4. Opens pi on the provider and model from the wizard.
5. For engines it has no launcher for (vLLM, Ollama, ...), warns and opens pi anyway.

`./harness status` shows what pi is pointed at.

In the session: `read`/`edit`/`bash` as usual, `mcp__semble__search` for "where is X implemented", `commit` for a
sidecar-written message, `triage` when a decision is a one-word call, and `/tune` to change compaction headroom, output
budget, temperature or ubatch mid-session. Codebase search needs no setup: semble indexes on first query.

The default halogen launch is its native max (**262,144 ctx x 4 slots**) behind an allocator pre-flight gate. If order-9
pages are low it recovers or falls back to 65k/2-slot and prints the exact fix command. Context knobs, the `vm-compact.timer` and the
small-context opt-out are in [Operations](OPS.md).

### Crash recovery

Kill pi or the server mid-task, then relaunch with `./start.sh <dir> -c` (pi's continue: the last session in that
directory, whole) and say "continue". `progress-tracker` also writes a checkpoint (task, last state, changed files) to
`~/.pi/agent/progress/` for you to read; it is not fed to the model, so it costs no context and no cache miss (set
`PI_PROGRESS_FILE` per task for parallel work, `/progress-reset` to clear it).

## Uninstall

```sh
./setup.sh --uninstall
```

Removes everything the installer provisioned (extensions, templates, theme, any skills older releases copied, pi defaults, the providers and the
wizard's profile files where they still match what setup set, and the systemd units) after backing all of it up to
`~/pi-harness-uninstall-backup-*.tar.gz`. The repo ships no skills itself; the pi packages bring their own. **Deliberately left alone:** your own model entries, `mcp.json`, the sidecar
GGUF (the reclaim command is printed), the halogen container and your checkpoint files.
