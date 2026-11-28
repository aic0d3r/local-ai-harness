// run: bun test tests/   (prompt-lint core: stale refs and duplicates found, hard rules never deleted, diff is deletion-only)
import { expect, test } from "bun:test";
import { applyDeletions, blocks, budget, lint, type Source } from "../extensions/prompt-lint/core.ts";

const env = { servers: { serena: true, playwright: false }, tools: ["mcp__serena__find_symbol"], exists: (p: string) => p === "~/real.json" };
const src = (name: string, text: string, editable = true, group = "live"): Source => ({ name, text, editable, group });

test("blocks join wrapped list items and skip headings", () => {
	const b = blocks("# H\n\n- one\n  wrapped\n- two\n| a | b |\n| c | d |\n\npara\nline");
	expect(b.map((x) => [x.start, x.end])).toEqual([[2, 3], [4, 4], [5, 5], [6, 6], [8, 9]]);
});

test("stale MCP servers, missing tools and missing paths are flagged; live ones and slash commands are not", () => {
	const f = lint([src("A.md", [
		"- use `mcp__serena__find_symbol` for symbols",
		"- use `mcp__serena__gone_tool` too",
		"- browse with mcp__playwright__click",
		"- search with mcp__jina__search",
		"- config is in `~/real.json`, old one in `~/old/opencode.json`",
		"- toggle with `/mcp` or `/plan`",
	].join("\n"))], env);
	expect(f.map((x) => [x.line, x.msg])).toEqual([
		[2, "mcp__serena__gone_tool is not a tool of serena"],
		[3, 'MCP server "playwright" is disabled'],
		[4, 'MCP server "jina" is not configured'],
		[5, "path ~/old/opencode.json does not exist"],
	]);
	expect(f.every((x) => x.del)).toBe(true);
});

test("tool names are not checked for a server whose tools have not loaded yet", () => {
	expect(lint([src("A.md", "- mcp__serena__anything")], { ...env, tools: [] })).toEqual([]);
});

test("a hard rule is reported but never proposed for deletion", () => {
	const f = lint([src("A.md", "- NEVER call mcp__playwright__click")], env);
	expect(f).toHaveLength(1);
	expect(f[0].del).toBeUndefined();
});

test("duplicates keep the copy the user can't edit and only compare within a group", () => {
	const rule = "Search with semble first for any question about where code lives in the repo";
	const f = lint([src("A.md", `- ${rule}`), src("skill:x", `${rule}.`, false), src("T.md", `- ${rule}`, true, "template")], env);
	expect(f).toHaveLength(1);
	expect(f[0]).toMatchObject({ kind: "duplicate", source: "A.md", msg: "repeats skill:x:1" });
	expect(f[0].del).toBeDefined();
	// short lines (under 8 words) are too generic to call duplicates
	expect(lint([src("A.md", "- be concise"), src("B.md", "- be concise")], env)).toEqual([]);
});

test("ids survive line moves so the startup nudge only fires for new findings", () => {
	const a = lint([src("A.md", "- mcp__jina__x")], env)[0].id;
	const b = lint([src("A.md", "# new heading\n\nintro\n\n- mcp__jina__x")], env)[0].id;
	expect(a).toBe(b);
});

test("budget warns only over 10% of the cold prompt", () => {
	expect(budget(1000, 20000)).toBeNull();
	expect(budget(6000, 32000)?.msg).toContain("19% of a ~8,000-token cold prompt");
});

test("deletions drop whole blocks, leave no double blank line and list the removed lines", () => {
	const r = applyDeletions("# H\n\n- keep\n\n- drop\n  this\n\n- keep2", [{ start: 4, end: 5, text: "" }]);
	expect(r.text).toBe("# H\n\n- keep\n\n- keep2");
	expect(r.diff).toBe("-   5 - drop\n-   6   this");
	expect(applyDeletions("a\n\n\nb", []).text).toBe("a\n\n\nb"); // untouched spacing stays as the user wrote it
});
