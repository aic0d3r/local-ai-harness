// prompt-lint: flags instruction fluff that piles up over time, without a model and without touching the prompt.
//  - stale: mcp__<server>__<tool> references to servers that are missing or disabled in mcp.json (or tools that
//    server doesn't have), and backticked ~/ or / paths that don't exist
//  - duplicate: the same rule, near word-for-word, in two loaded files (or a file and a skill description)
//  - budget: AGENTS.md files + APPEND_SYSTEM over 10% of the cold prompt (system prompt + tool declarations)
// Sources: loaded AGENTS.md files, APPEND_SYSTEM.md, skill descriptions (report only), and the repo's
// config/ templates when pi runs inside the harness repo. /trim shows the report and a deletion-only diff,
// applied only on a yes (originals kept as .bak). Lines with MUST/NEVER/"hard rule" are never proposed.
// At the first prompt of a session a footer line appears only for findings /trim hasn't shown yet.
// Off switch: PROMPT_LINT_DISABLED=1.
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { type ExtensionAPI, getAgentDir } from "@earendil-works/pi-coding-agent";
import { applyDeletions, type Block, budget, type Finding, lint, type Source, tok } from "./core.ts";
import { ev } from "../harness-profile/index.ts";

const AGENT = getAgentDir();
const SEEN = path.join(AGENT, "prompt-lint-seen.json");
const home = (p: string) => (p.startsWith("~/") ? path.join(os.homedir(), p.slice(2)) : p);
const pretty = (p: string) => (p.startsWith(os.homedir() + path.sep) ? "~" + p.slice(os.homedir().length) : p);
const read = (p: string) => { try { return fs.readFileSync(p, "utf8"); } catch { return null; } };
const json = (p: string) => { try { return JSON.parse(fs.readFileSync(p, "utf8")); } catch { return {}; } };

function servers(cwd: string): Record<string, boolean> {
	const out: Record<string, boolean> = {};
	// a project entry may only override `enabled` (pi docs/mcp.md), so merge global then project
	for (const f of [path.join(AGENT, "mcp.json"), path.join(cwd, ".pi", "mcp.json")])
		for (const [k, v] of Object.entries<any>(json(f).mcpServers ?? {})) out[k] = v?.enabled ?? out[k] ?? true;
	return out;
}

function gather(pi: ExtensionAPI, opts: any, systemPrompt: string) {
	const cwd = opts?.cwd ?? process.cwd();
	const files = new Map<string, string>(); // source name -> path on disk
	const sources: Source[] = [];
	const addFile = (p: string, group: string) => {
		const text = read(p);
		if (text == null || files.has(pretty(p))) return;
		files.set(pretty(p), p);
		sources.push({ name: pretty(p), text, editable: true, group });
	};
	for (const f of opts?.contextFiles ?? []) addFile(f.path, "live");
	for (const p of [path.join(AGENT, "APPEND_SYSTEM.md"), path.join(cwd, ".pi", "APPEND_SYSTEM.md")]) if (opts?.appendSystemPrompt) addFile(p, "live");
	for (const s of opts?.skills ?? []) if (!s.disableModelInvocation) sources.push({ name: `skill:${s.name}`, text: s.description ?? "", editable: false, group: "live" });
	const repo = read(path.join(AGENT, "harness-repo"))?.trim();
	if (repo && (cwd + path.sep).startsWith(repo + path.sep)) for (const t of ["AGENTS.md", "APPEND_SYSTEM.md"]) addFile(path.join(repo, "config", t), "template");

	const all = pi.getAllTools();
	const hidden = new Set(opts?.hiddenTools ?? []), active = new Set(pi.getActiveTools());
	const declared = all.filter((t: any) => active.has(t.name) && !hidden.has(t.name));
	const toolChars = JSON.stringify(declared.map((t: any) => ({ name: t.name, description: t.description, parameters: t.parameters }))).length;
	const instrChars = (opts?.contextFiles ?? []).reduce((n: number, f: any) => n + (f.content?.length ?? 0), 0) + (opts?.appendSystemPrompt?.length ?? 0);
	const coldChars = systemPrompt.length + toolChars;
	const findings: Finding[] = lint(sources, { servers: servers(cwd), tools: all.map((t: any) => t.name), exists: (p) => fs.existsSync(home(p)) });
	const b = budget(instrChars, coldChars);
	if (b) findings.push(b);
	return { sources, files, findings, instrChars, coldChars, toolChars, tools: declared.length };
}

const seen = (): Set<string> => new Set(Array.isArray(json(SEEN)) ? json(SEEN) : []);

export default function promptLint(pi: ExtensionAPI) {
	let checked = false;
	pi.on("session_start", async () => { checked = false; });

	// first prompt only: by then MCP servers have usually connected. Reads files, returns nothing: the prompt is untouched.
	pi.on("before_agent_start", async (event: any, ctx: any) => {
		if (checked || process.env.PROMPT_LINT_DISABLED) return;
		checked = true;
		try {
			const known = seen();
			const fresh = gather(pi, event.systemPromptOptions, ctx.getSystemPrompt()).findings.filter((f) => !known.has(f.id));
			if (fresh.length) ctx.ui?.setStatus?.("prompt-lint", `prompt-lint: ${fresh.length} new finding${fresh.length > 1 ? "s" : ""}, run /trim`);
		} catch {}
	});

	pi.registerCommand("trim", {
		description: "Check AGENTS.md, APPEND_SYSTEM and skills for stale, duplicate or oversized instructions; offers a diff",
		handler: async (_args, ctx: any) => {
			const g = gather(pi, ctx.getSystemPromptOptions(), ctx.getSystemPrompt());
			fs.writeFileSync(SEEN, JSON.stringify(g.findings.map((f) => f.id)));
			ctx.ui.setStatus?.("prompt-lint", undefined);

			const size = [
				`instruction files ~${tok(g.instrChars)} tokens of a ~${tok(g.coldChars)}-token cold prompt (tool declarations ~${tok(g.toolChars)} for ${g.tools} tools)`,
				...g.sources.filter((s) => s.editable && s.group === "live").map((s) => `  ${s.name}  ~${tok(s.text.length)}`),
				...g.sources.filter((s) => !s.editable).sort((a, b) => b.text.length - a.text.length).slice(0, 3).filter((s) => s.text.length > 300).map((s) => `  ${s.name} description ~${tok(s.text.length)} (longest skills)`),
			];
			const lines = g.findings.map((f) => `${f.kind.padEnd(10)} ${f.line ? `${f.source}:${f.line}` : f.source}  ${f.msg}${f.del ? "" : f.kind === "budget" ? "" : g.files.has(f.source) ? "  [report only: hard rule]" : "  [report only]"}`);
			const dels = new Map<string, Block[]>();
			for (const f of g.findings) if (f.del) dels.set(f.source, [...(dels.get(f.source) ?? []), f.del]);
			const plans = [...dels].map(([name, blocks]) => {
				const before = g.sources.find((s) => s.name === name)!.text;
				return { name, file: g.files.get(name)!, before, ...applyDeletions(before, blocks) };
			});
			ev({ c: "prompt-lint", k: "report", findings: g.findings.length, deletable: plans.length });
			const report = [g.findings.length ? `${g.findings.length} finding(s):` : "no stale or duplicate instructions found", ...lines, "", ...size].join("\n");
			if (!plans.length) return ctx.ui.notify(report, "info");
			ctx.ui.notify(`${report}\n\nproposed diff (deletions only):\n${plans.map((p) => `--- ${p.name}\n${p.diff}`).join("\n")}`, "info");
			if (!ctx.hasUI || !(await ctx.ui.confirm("Apply the prompt-lint diff?", `${plans.length} file(s); originals are kept as .bak`))) return;

			const done: string[] = [];
			for (const p of plans) {
				if (read(p.file) !== p.before) { ctx.ui.notify(`${p.name} changed since the check: skipped, run /trim again`, "warning"); continue; }
				fs.copyFileSync(p.file, `${p.file}.bak`);
				fs.writeFileSync(p.file, p.text);
				done.push(p.name);
			}
			ev({ c: "prompt-lint", k: "applied", files: done.length, saved: plans.reduce((n, p) => n + p.before.length - p.text.length, 0) });
			if (done.length) ctx.ui.notify(`updated ${done.join(", ")} (.bak kept). Takes effect in a new session or after /reload.`, "info");
		},
	});
}
