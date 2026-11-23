// harness-status: /usage runs scripts/usage-stats.py inside pi; a footer status shows halogen load.
//   /usage [--all] [--top N]     (HARNESS_REPO overrides the repo path)
// Footer: "halogen kv 14% · 1 run · 0 wait · 62 t/s" polled from :8731/metrics every 5s; hidden when unreachable.
// Any other provider: "speed pf 1.2k/s · 41 t/s" timed client-side per reply (gufo, osmos, ollama... have no usable
// /metrics), each reply appended to <agent dir>/harness-speed.jsonl for `./harness monitor`.
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { type ExtensionAPI, FooterComponent, getAgentDir } from "@earendil-works/pi-coding-agent";
import { truncateToWidth } from "@earendil-works/pi-tui";
import { profile, sidecarOn, weights, ev } from "../harness-profile/index.ts";
import { otherOwner } from "./session-lock.ts";

const BASE = process.env.HALOGEN_URL || "http://127.0.0.1:8731";
// setup.sh writes the repo path to <agent dir>/harness-repo
const recorded = (() => { try { return fs.readFileSync(path.join(getAgentDir(), "harness-repo"), "utf8").trim(); } catch { return ""; } })();
const REPO = process.env.HARNESS_REPO || recorded || path.join(os.homedir(), "coding", "local-ai-harness");
const metric = (txt: string, k: string) => { const m = txt.match(new RegExp(`^${k.replace(":", ":")}\\s+([0-9.eE+-]+)`, "m")); return m ? Number(m[1]) : undefined; };

// pi's footer prints model.id on the right and offers no hook for it: wrap its render once and swap "(provider) id" for the label.
// The label lives on globalThis so a /reload updates it without wrapping twice.
const FOOTER_LABEL = Symbol.for("harness-status.footer-label");
export const swapModel = (line: string, provider: string, id: string, text: string, width: number) => {
	for (const old of [`(${provider}) ${id}`, id]) {
		const i = line.lastIndexOf(old);
		if (i < 0) continue;
		// eat the padding in front so the right edge stays put; truncate when it is not enough
		const pad = line.slice(0, i).match(/ *$/)![0].length, eat = Math.max(0, Math.min(text.length - old.length, pad - 2));
		return truncateToWidth(line.slice(0, i - eat) + text + line.slice(i + old.length), width, "");
	}
	return line;
};
const proto: any = FooterComponent.prototype;
if (!proto[FOOTER_LABEL]) {
	proto[FOOTER_LABEL] = true;
	const render = proto.render;
	proto.render = function (this: any, width: number) {
		const lines: string[] = render.call(this, width), l = (globalThis as any)[FOOTER_LABEL], m = this.session?.state?.model;
		if (l && m?.provider === l.provider && lines[1]) lines[1] = swapModel(lines[1], m.provider, m.id, l.text, width);
		return lines;
	};
}

export default function harnessStatus(pi: ExtensionAPI) {
	let timer: ReturnType<typeof setInterval> | undefined;
	let prev: { tok: number; sec: number } | undefined;
	let rate: number | undefined;

	pi.registerCommand("usage", {
		description: "MCP / skill / extension usage report (scripts/usage-stats.py); pass --all for lifetime",
		handler: async (args: string, ctx: any) => {
			ev({ c: "harness-status", k: "usage" });
			const r = await pi.exec("python3", [path.join(REPO, "scripts/usage-stats.py"), ...(args ?? "").split(/\s+/).filter(Boolean)], { cwd: REPO, timeout: 30_000 });
			ctx.ui.notify((r.stdout || r.stderr || "no output").trim(), r.code === 0 ? "info" : "error");
		},
	});

	const poll = async (ctx: any) => {
		try {
			const txt = await (await fetch(`${BASE}/metrics`, { signal: AbortSignal.timeout(2000) })).text();
			const kv = metric(txt, "llamacpp:kv_cache_usage_ratio"), run = metric(txt, "llamacpp:requests_processing"), wait = metric(txt, "halogen:kv_pool_waiting_for_room");
			const ptok = metric(txt, "llamacpp:prompt_tokens_total"), psec = metric(txt, "llamacpp:prompt_seconds_total");
			const tok = metric(txt, "llamacpp:tokens_predicted_total"), sec = metric(txt, "llamacpp:tokens_predicted_seconds_total");
			if (tok !== undefined && sec !== undefined) {
				if (prev && sec > prev.sec) rate = (tok - prev.tok) / (sec - prev.sec); // last decode rate, kept while idle
				prev = { tok, sec };
			}
			// prefill is bursty, so a poll delta is useless: show the since-start average, the number ubatch/batch tuning moves
			const pf = ptok && psec ? ptok / psec : undefined;
			const k = (n: number) => (n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(Math.round(n)));
			const parts = [kv !== undefined ? `kv ${Math.round(kv * 100)}%` : "", run ? `${run} run` : "", wait ? `${wait} wait` : "", pf ? `pf ${k(pf)}/s` : "", rate ? `${k(rate)} t/s` : ""].filter(Boolean);
			ctx.ui.setStatus("halogen", parts.length ? `halogen ${parts.join(" · ")}` : undefined);
		} catch {
			ctx.ui.setStatus("halogen", undefined); // server down or not halogen: show nothing rather than stale numbers
		}
	};

	// footer + polling only while a halogen model is selected; other providers get nothing
	const stop = (ctx: any) => { clearInterval(timer); timer = undefined; prev = rate = undefined; ctx.ui.setStatus("halogen", undefined); ctx.ui.setStatus("speed", undefined); };
	// pi's footer shows the served id (osmos: osmos-osmos-evo); a one-model engine's profiles.json model_label + weights reads better
	const label = (provider: string | undefined, _ctx: any) => {
		let text: string | undefined;
		try {
			const e = JSON.parse(fs.readFileSync(path.join(REPO, "config/profiles.json"), "utf8")).engines?.[profile().engine];
			if (provider === profile().provider && e?.model_label) text = `${e.model_label} · ${weights()}`;
		} catch {}
		(globalThis as any)[FOOTER_LABEL] = text && { provider, text };
	};
	const sync = (provider: string | undefined, ctx: any) => {
		stop(ctx);
		label(provider, ctx);
		if (provider !== "halogen") return void ctx.ui.setStatus("speed", lastSpeed(provider));
		void poll(ctx);
		timer = setInterval(() => void poll(ctx), 5000);
		timer.unref?.();
	};
	// sidecar badge, any provider: llama-server sleeps (model unloaded) after 10 min idle, wakes on the next request
	let scTimer: ReturnType<typeof setInterval> | undefined;
	const scPoll = async (ctx: any) => {
		let s = "off";
		try {
			const p: any = await (await fetch(`${profile().sidecarUrl}/props`, { signal: AbortSignal.timeout(2000) })).json();
			s = p.is_sleeping ? "asleep" : "on";
		} catch {}
		ctx.ui.setStatus("sidecar", ctx.ui.theme.fg("muted", `sidecar ${s}`));
	};
	const scStop = (ctx: any) => { clearInterval(scTimer); scTimer = undefined; ctx.ui.setStatus("sidecar", undefined); };

	// client-side speed: prefill = uncached prompt tokens / time to first stream event; decode = output / the rest.
	// Time to first event includes queueing and network, so prefill reads a little low.
	let t0 = 0, t1 = 0, deltas = 0;
	const speedLog = path.join(getAgentDir(), "harness-speed.jsonl");
	const k = (n: number) => (n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(Math.round(n)));
	// size and wait beside the rate: short agent-turn prefills read far below ./harness tune's 8K figure (osmos: a 562-token prompt still streams 12 GB of experts)
	const speedText = (pf: number | undefined, pt: number, ttft: number, tps: number | undefined) =>
		`speed ${[pf ? `pf ${k(pf)}/s (${k(pt)} tok, ${ttft.toFixed(1)}s)` : "", tps ? `${k(tps)} t/s` : ""].filter(Boolean).join(" · ")}`;
	// a model switch or restart clears the line: show the provider's last measured reply again instead of nothing
	const lastSpeed = (provider: string | undefined) => {
		let lines: string[] = [];
		try { lines = fs.readFileSync(speedLog, "utf8").trim().split("\n"); } catch {}
		for (let i = lines.length - 1; i >= 0; i--) {
			try {
				const r = JSON.parse(lines[i]);
				if (r.provider === provider && (r.pf_tps || r.tps)) return speedText(r.pf_tps, r.prompt, r.ttft, r.tps);
			} catch {} // a torn line from a crash mid-append
		}
		return undefined;
	};
	pi.on("before_provider_request", async () => { t0 = Date.now(); t1 = 0; deltas = 0; });
	// only real tokens: osmos sends an empty role chunk at 0.01s, long before prefill ends
	pi.on("message_update", async (e: any) => {
		if (!t0 || !String(e.assistantMessageEvent?.type).endsWith("_delta")) return;
		deltas++;
		t1 ||= Date.now();
	});
	pi.on("message_end", async (e: any, ctx: any) => {
		const m = e.message;
		if (m?.role !== "assistant" || !t0 || !t1 || m.provider === "halogen" || m.stopReason === "error" || m.stopReason === "aborted") return;
		const u = m.usage ?? {}, t2 = Date.now(), pt = (u.input ?? 0) + (u.cacheWrite ?? 0);
		const ttft = (t1 - t0) / 1000, dec = (t2 - t1) / 1000;
		t0 = 0;
		// a reply that arrived in one burst (osmos sends a whole tool call in one chunk) hides where prefill ended: no rates
		const streamed = deltas >= 3 && dec >= 0.25;
		const pf = streamed && pt >= 256 ? pt / ttft : undefined; // tiny uncached tails make a meaningless rate
		const tps = streamed && u.output >= 16 ? u.output / dec : undefined;
		if (pf || tps) ctx.ui.setStatus("speed", speedText(pf, pt, ttft, tps));
		try {
			// ~200 B per reply and lastSpeed reads it all: past 1 MiB keep the newest 2,500 lines, renamed in whole (./harness monitor reopens on a new inode)
			if ((fs.statSync(speedLog, { throwIfNoEntry: false })?.size ?? 0) > 1 << 20) {
				const tmp = `${speedLog}.${process.pid}.tmp`;
				fs.writeFileSync(tmp, fs.readFileSync(speedLog, "utf8").split("\n").slice(-2501).join("\n"));
				fs.renameSync(tmp, speedLog);
			}
			fs.appendFileSync(speedLog, JSON.stringify({ t: new Date(t2).toISOString(), provider: m.provider, model: m.model, prompt: pt, cached: u.cacheRead ?? 0,
				ttft: +ttft.toFixed(2), out: u.output ?? 0, decode_s: +dec.toFixed(2), deltas, pf_tps: pf && +pf.toFixed(0), tps: tps && +tps.toFixed(1) }) + "\n");
		} catch {}
	});

	// one session, two pi processes (Paseo's background agent plus `pi -c` in a terminal) both append to the same session
	// file and run the same work twice: warn the second one
	let lock: string | undefined;
	const lockFor = (f: string) => path.join(getAgentDir(), "session-locks", `${path.basename(f)}.pid`);
	pi.on("session_start", async (_e: any, ctx: any) => {
		const f = ctx.sessionManager?.getSessionFile?.();
		if (f) {
			lock = lockFor(f);
			const other = otherOwner(lock);
			if (other) ctx.ui.notify(`This session is also open in pi pid ${other} (Paseo or another terminal?). Both write the same session file and repeat the same work: close one (kill ${other}).`, "warning");
			try { fs.mkdirSync(path.dirname(lock), { recursive: true }); fs.writeFileSync(lock, String(process.pid)); } catch {}
		}
		sync(ctx.model?.provider, ctx);
		scStop(ctx);
		if (!sidecarOn()) return;
		void scPoll(ctx);
		scTimer = setInterval(() => void scPoll(ctx), 10_000);
		scTimer.unref?.();
	});
	pi.on("model_select", async (e: any, ctx: any) => sync(e.model?.provider, ctx));
	pi.on("session_shutdown", async (_e: any, ctx: any) => {
		stop(ctx); scStop(ctx);
		try { if (lock && fs.readFileSync(lock, "utf8").trim() === String(process.pid)) fs.unlinkSync(lock); } catch {}
		lock = undefined;
	});
}
