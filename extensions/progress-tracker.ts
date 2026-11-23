/**
 * progress-tracker.ts — verified against the real pi -p event shapes:
 *   session_start:          {type, reason}          — no messages (v2/v3 injection was a silent no-op)
 *   before_agent_start:     {type, prompt, ...}     — prompt = the task; MUTABLE (injection point)
 *   after_provider_response:{type, status, headers} — no messages
 *
 * Checkpoint only: the task label comes from event.prompt, the file is written for a human (or a
 * script) to read. Nothing is injected into the model's context: resuming is pi's own `pi -c` / /resume,
 * which restores the whole conversation (an injected note also cost a prefix-cache miss on local engines).
 */

import * as crypto from "node:crypto";
import * as fs from "node:fs";
import * as path from "node:path";
import * as os from "node:os";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const PROGRESS_FILE = process.env.PI_PROGRESS_FILE || "PROGRESS.md";
// pi's own config-dir rule (kept inline so bench/check-fixes.sh can import this file outside pi)
const AGENT_DIR = process.env.PI_CODING_AGENT_DIR || path.join(os.homedir(), ".pi", "agent");
const WRITE_INTERVAL = 5;

// a continuation keeps the stored task label: short prompts naming one, or any prompt that
// STARTS with one ("Continue where we left off. <long explanation>")
export function isContinuation(t: string): boolean {
	if (/^(continue|finish|keep going|resume|carry on)\b/i.test(t)) return true;
	return t.length > 0 && t.length < 120 && /\b(continue|finish|keep going|resume|carry on)\b/i.test(t);
}

// Only a PROGRESS.md this harness wrote is read back (its task label) or deleted: a cloned or unzipped repo can
// ship its own. The sha256 of every file we write is kept under the agent dir, one file per checkpoint path (not one
// shared index: two pi processes wrote that at once, last writer won).
const sha = (s: string) => crypto.createHash("sha256").update(s).digest("hex");
const stampFile = (file: string) => path.join(AGENT_DIR, "progress", sha(file).slice(0, 16) + ".sha");
export const isOurs = (file: string, content: string): boolean => { try { return fs.readFileSync(stampFile(file), "utf8") === sha(content); } catch { return false; } };
export const stamp = (file: string, content: string) => { try { fs.mkdirSync(path.dirname(stampFile(file)), { recursive: true }); fs.writeFileSync(stampFile(file), sha(content)); } catch {} };

// Checkpoints live under the agent dir, one file per project folder: project folders stay clean (nothing to gitignore, nothing for
// `git add -A` to pick up). PI_PROGRESS_FILE still names a file inside the project when set (one per task for parallel work).
export const progressPath = (cwd: string) =>
	process.env.PI_PROGRESS_FILE ? path.join(cwd, process.env.PI_PROGRESS_FILE) : path.join(AGENT_DIR, "progress", sha(cwd).slice(0, 16) + ".md");

function promptText(p: any): string {
	if (typeof p === "string") return p;
	if (Array.isArray(p)) return p.map((b: any) => (typeof b === "string" ? b : b?.text ?? "")).join("\n");
	return "";
}

export default function (pi: ExtensionAPI) {
	const DEBUG = !!process.env.PI_PROGRESS_VERBOSE;
// subagent processes must not write checkpoints (last-writer-wins race on PROGRESS.md)
if (process.env.PI_SUBAGENT === "1") {
	return;
}
let responseCount = 0;
	let taskDescription = "";

	pi.on("session_start", async (event: any, ctx: any) => {
		let p = progressPath(process.cwd());
		// a PROGRESS.md an older version left in the project is read once (until the first checkpoint writes the new file), then ignored
		if (!fs.existsSync(p)) p = path.join(process.cwd(), PROGRESS_FILE);
		if (!fs.existsSync(p)) return;
		const content = fs.readFileSync(p, "utf8");
		if (content.trim().length <= 50) return;
		if (!isOurs(p, content)) { if (DEBUG) console.log(`[progress-tracker] ${PROGRESS_FILE} was not written by this harness - ignored`); return; }
		const t = content.match(/\*\*Task:\*\*\s*([\s\S]*?)\n\*\*Last updated/);
		if (t && t[1].trim() && t[1].trim() !== "(unspecified)") taskDescription = t[1].trim();
	});

	pi.on("before_agent_start", async (event: any, ctx: any) => {
		// task resolution: a substantial prompt = NEW task (a new session in a repo with an old
		// checkpoint must not inherit the old label). Only continuation-like prompts keep the stored
		// task, and short questions ("what were we doing?") never overwrite it.
		const t = promptText(event?.prompt).trim();
		const cont = isContinuation(t);
		if (t && t.length >= 120 && !cont && taskDescription !== t.slice(0, 2000)) {
			taskDescription = t.slice(0, 2000);
		} else if (!taskDescription && t) {
			taskDescription = t.slice(0, 2000);
		}
		// v6.3: checkpoint at agent start - a kill during response 1 still leaves the task on disk
		await writeProgress(ctx);
	});

	// this session's own branch: the newest .jsonl in the folder belonged to whichever
	// session in this directory wrote last, and re-reading it cost a full transcript parse
	function lastAssistantText(ctx: any): string {
		try {
			const entries: any[] = ctx?.sessionManager?.getBranch?.() ?? [];
			for (let i = entries.length - 1; i >= 0; i--) {
				const m = entries[i]?.message;
				if (entries[i]?.type === "message" && m?.role === "assistant") {
					for (const b of m.content || []) {
						if (b?.type === "text" && b.text?.trim()) return b.text.trim().slice(0, 1200);
					}
				}
			}
		} catch { /* no session manager */ }
		return "";
	}

	async function writeProgress(ctx: any) {
		const cwd = process.cwd();
		const { code, stdout } = await pi.exec("git", ["diff", "--stat"], { cwd });
		// diff --stat only shows tracked files: a session that only creates new
		// files checkpointed as "(no changes)", which crash recovery must not miss
		const { code: sc, stdout: ss } = await pi.exec("git", ["status", "--short"], { cwd });
		const pb = path.basename(PROGRESS_FILE);
		const statusLines = sc === 0 ? ss.split("\n").filter((l: string) => l.trim() && !l.includes(pb) && !l.includes(".progress-log")).join("\n") : "";
		const diffStat = [code === 0 ? stdout.trim() : "", statusLines].filter(Boolean).join("\n") || "(no changes)";
		const { code: lc, stdout: ls } = await pi.exec("git", ["log", "--oneline", "-3"], { cwd });
		const recentCommits = lc === 0 ? ls.trim() : "";
		const taskLine = (taskDescription || "(unspecified)").replace(/\n+/g, " ").slice(0, 2000);
		const lastNote = lastAssistantText(ctx);
		const entry = `# Session Progress\n\n**Task:** ${taskLine}\n**Last updated:** ${new Date().toISOString()}\n**Responses:** ${responseCount}\n\n## Last state (assistant note)\n\n${lastNote || "(none yet)"}\n\n## Files modified\n\n\`\`\`\n${diffStat}\n\`\`\`\n\n## Recent commits\n\n\`\`\`\n${recentCommits}\n\`\`\`\n`;
		try {
			const file = progressPath(cwd);
			fs.mkdirSync(path.dirname(file), { recursive: true });
			fs.writeFileSync(file, entry);
			stamp(file, entry);
			if (DEBUG) console.log(`[progress-tracker] wrote ${PROGRESS_FILE} at response ${responseCount}`);
		} catch {}
	}

	pi.on("after_provider_response", async (event: any, ctx: any) => {
		responseCount++;
		if (responseCount === 1 || responseCount % WRITE_INTERVAL === 0) await writeProgress(ctx);
	});

	pi.registerCommand("progress-reset", {
		description: "Forget this project's saved progress checkpoint (its task label)",
		handler: async (_args: any, ctx: any) => {
			const cwd = process.cwd();
			for (const f of [progressPath(cwd), path.join(cwd, PROGRESS_FILE)]) {
				// only files this harness wrote: a PROGRESS.md the project owns is not ours to delete
				try { if (isOurs(f, fs.readFileSync(f, "utf8"))) { fs.rmSync(f); fs.rmSync(stampFile(f), { force: true }); } } catch {}
			}
			taskDescription = "";
			ctx.ui.notify("progress note cleared for this project", "info");
		},
	});

	pi.on("session_shutdown", async (event: any, ctx: any) => {
		if (responseCount > 0) await writeProgress(ctx);
	});
}
