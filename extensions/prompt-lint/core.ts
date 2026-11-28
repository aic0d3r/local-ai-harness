// Pure checks for prompt-lint: no model, no I/O (the caller passes `exists`). Every finding is either
// report-only or a whole-block deletion; a block that states a hard rule is never proposed for deletion.
import * as crypto from "node:crypto";

export type Source = { name: string; text: string; editable: boolean; group: string }; // dups are only compared within a group
export type Block = { start: number; end: number; text: string }; // 0-based inclusive line range
export type Finding = { id: string; kind: "stale-mcp" | "stale-path" | "duplicate" | "budget"; source: string; line: number; msg: string; del?: Block };
export type Env = { servers: Record<string, boolean>; tools: string[]; exists: (p: string) => boolean };

export const PROTECTED = /\b(MUST|NEVER)\b|hard rules?/i;
export const BUDGET = 0.1; // instruction files above this share of the cold prompt get a warning
const MCP = /mcp__([A-Za-z0-9-]+(?:_[A-Za-z0-9-]+)*)__(\w*)/g;
const TICKED = /`([^`\n]+)`/g;
const MIN_WORDS = 8, SIMILAR = 0.8;

/** Markdown blocks: a list item or paragraph with its wrapped lines, one table row, or one code fence. Headings are never blocks. */
export function blocks(text: string): Block[] {
	const lines = text.split("\n"), out: Block[] = [];
	let cur: Block | null = null;
	const close = () => { if (cur) out.push(cur); cur = null; };
	for (let i = 0; i < lines.length; i++) {
		const l = lines[i];
		if (/^\s*```/.test(l)) {
			close();
			let j = i + 1;
			while (j < lines.length && !/^\s*```/.test(lines[j])) j++;
			out.push({ start: i, end: Math.min(j, lines.length - 1), text: lines.slice(i, j + 1).join("\n") });
			i = j;
			continue;
		}
		if (!l.trim() || /^#{1,6}\s/.test(l)) { close(); continue; }
		const starts = /^\s*([-*+]|\d+[.)])\s/.test(l) || /^\s*\|/.test(l);
		if (starts || !cur) { close(); cur = { start: i, end: i, text: l }; }
		else { cur.end = i; cur.text += "\n" + l; }
		if (/^\s*\|/.test(l)) close();
	}
	close();
	return out;
}

const words = (t: string) => new Set(t.toLowerCase().replace(/[^a-z0-9_\s]/g, " ").split(/\s+/).filter((w) => w.length > 2));
const jaccard = (a: Set<string>, b: Set<string>) => { let n = 0; for (const w of a) if (b.has(w)) n++; return n / (a.size + b.size - n); };
const hash = (...p: string[]) => crypto.createHash("sha1").update(p.join("\0")).digest("hex").slice(0, 12);
const del = (s: Source, b: Block) => (s.editable && !PROTECTED.test(b.text) ? b : undefined);

export function lint(sources: Source[], env: Env): Finding[] {
	const out: Finding[] = [];
	const loaded = new Set(env.tools.filter((t) => t.startsWith("mcp__")).map((t) => t.replace(MCP, "$1")));
	const tools = new Set(env.tools);
	for (const s of sources) {
		for (const b of blocks(s.text)) {
			const why: string[] = [];
			for (const [, server, tool] of b.text.matchAll(MCP)) {
				if (!(server in env.servers)) why.push(`MCP server "${server}" is not configured`);
				else if (!env.servers[server]) why.push(`MCP server "${server}" is disabled`);
				else if (tool && loaded.has(server) && !tools.has(`mcp__${server}__${tool}`)) why.push(`mcp__${server}__${tool} is not a tool of ${server}`);
			}
			for (const [, p] of b.text.matchAll(TICKED)) {
				// only literal home or multi-segment absolute paths (`/plan` is a slash command); placeholders, globs and
				// project-relative paths can't be checked here
				if (!/^(~\/\S|\/[^/\s]+\/)/.test(p) || /[*<>{}…$]|\.\.\./.test(p) || env.exists(p)) continue;
				why.push(`path ${p} does not exist`);
			}
			const uniq = [...new Set(why)];
			if (uniq.length) out.push({ id: hash("stale", s.name, b.text), kind: uniq[0].startsWith("path") ? "stale-path" : "stale-mcp", source: s.name, line: b.start + 1, msg: uniq.join("; "), del: del(s, b) });
		}
	}
	// duplicates: keep the first copy, preferring a source the user can't edit (a skill), propose deleting the rest
	const all = sources.flatMap((s) => blocks(s.text).map((b) => ({ s, b, w: words(b.text) }))).filter((x) => x.w.size >= MIN_WORDS);
	const taken = new Set<number>();
	for (let i = 0; i < all.length; i++) {
		if (taken.has(i)) continue;
		const group = [i];
		for (let j = i + 1; j < all.length; j++) if (!taken.has(j) && all[j].s.group === all[i].s.group && jaccard(all[i].w, all[j].w) >= SIMILAR) group.push(j);
		if (group.length < 2) continue;
		group.forEach((g) => taken.add(g));
		const keep = group.find((g) => !all[g].s.editable) ?? group[0];
		const k = all[keep];
		for (const g of group) {
			if (g === keep) continue;
			const { s, b } = all[g];
			out.push({ id: hash("dup", s.name, b.text), kind: "duplicate", source: s.name, line: b.start + 1, msg: `repeats ${k.s.name}:${k.b.start + 1}`, del: del(s, b) });
		}
	}
	return out;
}

/** Warns when the always-loaded instruction text (AGENTS.md files + APPEND_SYSTEM) is over BUDGET of the cold prompt. */
export function budget(instrChars: number, coldChars: number): Finding | null {
	if (!coldChars || instrChars / coldChars <= BUDGET) return null;
	const pct = Math.round((100 * instrChars) / coldChars);
	return { id: hash("budget", String(Math.round(pct / 5))), kind: "budget", source: "prompt", line: 0, msg: `instruction files are ~${tok(instrChars)} tokens, ${pct}% of a ~${tok(coldChars)}-token cold prompt (budget ${BUDGET * 100}%)` };
}

export const tok = (chars: number) => Math.round(chars / 4).toLocaleString("en-US"); // shortcut: chars/4 estimate, not the model's tokenizer

/** Removes the given blocks and returns the new text plus a deletion-only diff (line numbers are the original's). */
export function applyDeletions(text: string, dels: Block[]): { text: string; diff: string } {
	const lines = text.split("\n"), drop = new Set<number>();
	for (const b of dels) for (let i = b.start; i <= b.end; i++) drop.add(i);
	const diff = [...drop].sort((a, b) => a - b).map((i) => `-${String(i + 1).padStart(4)} ${lines[i]}`).join("\n");
	const kept: string[] = [];
	for (let i = 0; i < lines.length; i++) {
		if (drop.has(i)) continue;
		// a deleted block between two blank lines would leave a double blank: drop the second one
		if (i > 0 && drop.has(i - 1) && !lines[i].trim() && !kept.at(-1)?.trim()) continue;
		kept.push(lines[i]);
	}
	return { text: kept.join("\n"), diff };
}
