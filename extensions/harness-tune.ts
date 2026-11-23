/**
 * /tune: adjust this harness's performance knobs from inside pi.
 *
 *   /tune                      show current values (read from the real files)
 *   /tune <key> <value>        set one key (validated, written immediately)
 *   /tune reset                restore defaults documented in the harness post
 *
 * Keys (friendly names map to config locations):
 *   compactAt     k tokens; auto-compaction fires past this (settings.json compaction.reserveTokens)
 *   maxTokens     per-turn output budget (models.json model entry)
 *   temperature   1.0 = Qwen3.8 card sampling; set 0 for deterministic benches (models.json samplingParams)
 *   ubatch        llama.cpp-side batch size (harness-ubatch file; ignored by the halogen server)
 */

import * as fs from "node:fs";
import * as path from "node:path";
import { type ExtensionAPI, getAgentDir } from "@earendil-works/pi-coding-agent";
import { profile } from "./harness-profile/index.ts";

const AGENT_DIR = getAgentDir();
const SETTINGS = path.join(AGENT_DIR, "settings.json");
const MODELS = path.join(AGENT_DIR, "models.json");
const UBATCH_FILE = path.join(AGENT_DIR, "harness-ubatch");
const MAIN_PROVIDER = profile().provider;
const MAIN_MODEL = profile().model;

// canonical post-setup values = what setup.sh provisions (keep in sync with setup/harness.py reserve_for):
// maxTokens 8192 because halogen hard-rejects bigger requests past ~32.7k ctx (halogen's cap only: another engine's model
// entry keeps its own maxTokens); compactAt follows the real window (defaultCompactAt): 202 on halogen's 262k
const DEFAULTS: Record<string, number> = { maxTokens: 8192, temperature: 1.0, ubatch: 4096 };
const KEYS = ["compactAt", "maxTokens", "temperature", "ubatch"];

const readJson = (p: string): any => (fs.existsSync(p) ? JSON.parse(fs.readFileSync(p, "utf8")) : {});
// temp + rename: a crash mid-write must not leave pi a truncated models.json; the temp keeps the file's mode (0600 stays 0600)
const writeJson = (p: string, v: any) => {
	const tmp = `${p}.${process.pid}.tmp`;
	fs.writeFileSync(tmp, JSON.stringify(v, null, 2) + "\n", { mode: fs.existsSync(p) ? fs.statSync(p).mode & 0o777 : 0o600 });
	fs.renameSync(tmp, p);
};
// the launcher syncs contextWindow on every start (262k normally, 65k on the
// allocator fallback); computing compactAt from the hardcoded 262k on a 65k
// day produced a reserve larger than the context - compaction never fired
const ctxWindow = (): number =>
	readJson(MODELS)?.providers?.[MAIN_PROVIDER]?.models?.find((x: any) => x?.id === MAIN_MODEL)?.contextWindow || 262144;
// a quarter of the window, at most 60k: the reserve setup.sh and ./start.sh set, so reset compacts a 1M osmos at ~989k
// (halogen's 202 moved it to ~202k, 5x earlier than the installer's own setting)
const reserveOf = (ctx: number) => Math.min(60000, Math.round(ctx / 4));
const defaultCompactAt = () => Math.round((ctxWindow() - reserveOf(ctxWindow())) / 1000);
const isHalogen = () => profile().engine === "halogen";

function current(): Record<string, number> {
	const s = readJson(SETTINGS);
	const m = readJson(MODELS);
	const entry = m?.providers?.[MAIN_PROVIDER]?.models?.find((x: any) => x?.id === MAIN_MODEL) || {};
	let ubatch = DEFAULTS.ubatch;
	if (fs.existsSync(UBATCH_FILE)) {
		const v = Number(fs.readFileSync(UBATCH_FILE, "utf8").trim());
		if (Number.isFinite(v)) ubatch = v;
	}
	return {
		compactAt: Math.round((ctxWindow() - (s?.compaction?.reserveTokens ?? 16384)) / 1000),
		maxTokens: entry?.maxTokens ?? DEFAULTS.maxTokens,
		temperature: entry?.samplingParams?.temperature ?? DEFAULTS.temperature,
		ubatch,
	};
}

const bounds = (): Record<string, [number, number]> => ({ compactAt: [10, Math.round((ctxWindow() - 10240) / 1000)], maxTokens: [1024, 8192], temperature: [0, 2], ubatch: [128, 8192] });

export default function (pi: ExtensionAPI) {
	pi.registerCommand("tune", {
		description: "Show or set harness tuning knobs (compactAt, maxTokens, temperature, ubatch)",
		handler: async (args, ctx) => {
			const argv = String(args ?? "").trim().split(/\s+/).filter(Boolean);

			if (argv.length === 0) {
				const c = current();
				ctx.ui.notify(
					`harness tuning (defaults: compactAt=${defaultCompactAt()}, ${Object.entries(DEFAULTS).map(([k, v]) => `${k}=${v}`).join(", ")}${isHalogen() ? "" : "; maxTokens is halogen's cap, not reset here"}):\n` +
						Object.entries(c).map(([k, v]) => `  ${k.padEnd(12)} ${v}`).join("\n") +
						`\nusage: /tune <key> <value>   (compactAt in k tokens; temperature 0 for reproducible benches; ubatch needs a server restart and 2048 is the deep-fill setting)`,
					"info",
				);
				return;
			}

			if (argv[0] === "reset") {
				// compactAt like the set path: computed from the real window, clamped to it (202k on a 65k window gave a negative reserve)
				const compactAt = Math.min(defaultCompactAt(), bounds().compactAt[1]);
				await apply("compactAt", compactAt);
				if (isHalogen()) await apply("maxTokens", DEFAULTS.maxTokens);
				await apply("temperature", DEFAULTS.temperature);
				await apply("ubatch", DEFAULTS.ubatch);
				ctx.ui.notify(`reset to defaults: compactAt=${compactAt}, temperature=1, ubatch=4096${isHalogen() ? ", maxTokens=8192" : " (maxTokens kept: 8192 is halogen's cap)"}`, "info");
				return;
			}

			const [key, val] = argv;
			const num = Number(val);
			if (!KEYS.includes(key)) {
				ctx.ui.notify(`unknown key '${key}'. keys: ${KEYS.join(", ")}`, "error");
				return;
			}
			if (!Number.isFinite(num) || num < 0) {
				ctx.ui.notify(`value must be a non-negative number, got '${val}'`, "error");
				return;
			}
			const [lo, hi] = bounds()[key];
			if (num < lo || num > hi) {
				ctx.ui.notify(`${key} must be between ${lo} and ${hi}`, "error");
				return;
			}
			await apply(key, num);
			ctx.ui.notify(`${key} = ${num} (restart pi if a session is mid-flight; server-side values apply next run)`, "info");
		},
	});

	async function apply(key: string, num: number) {
		if (key === "compactAt") {
			const s = readJson(SETTINGS);
			s.compaction = Object.assign({}, s.compaction, { enabled: true, reserveTokens: ctxWindow() - Math.round(num * 1000) });
			writeJson(SETTINGS, s);
		} else if (key === "maxTokens" || key === "temperature") {
			const m = readJson(MODELS);
			const entry = m?.providers?.[MAIN_PROVIDER]?.models?.find((x: any) => x?.id === MAIN_MODEL);
			if (!entry) throw new Error(`${MAIN_MODEL} not found in models.json`);
			if (key === "maxTokens") entry.maxTokens = Math.round(num);
			else {
				entry.samplingParams = Object.assign({}, entry.samplingParams, { temperature: num });
				if (num === 0) {
					entry.samplingParams.top_p = Math.max(entry.samplingParams.top_p ?? 0.95, 0.95);
					entry.samplingParams.min_p = entry.samplingParams.min_p ?? 0.05;
				}
			}
			writeJson(MODELS, m);
		} else if (key === "ubatch") {
			fs.writeFileSync(UBATCH_FILE, String(Math.round(num)));
		}
	}
}
