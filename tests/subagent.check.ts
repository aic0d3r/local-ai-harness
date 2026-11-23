// The subagent tool against a fake pi child. Run by tests/subagent.test.ts in its own process (`bun test` on this file):
// bun's mock.module is process-wide, so mocking node:child_process and the pi packages here would leak into every other
// test file. Not named *.test.ts on purpose, so `bun test tests/` does not pick it up directly.
import { expect, mock, test } from "bun:test";
import { EventEmitter } from "node:events";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { PassThrough } from "node:stream";

const AGENT = fs.mkdtempSync(path.join(os.tmpdir(), "sa-agent-"));
const CWD = fs.mkdtempSync(path.join(os.tmpdir(), "sa-proj-"));
fs.mkdirSync(path.join(AGENT, "agents"));
fs.writeFileSync(path.join(AGENT, "agents", "worker.md"), "---\nname: worker\ndescription: does things\n---\nYou are a worker.\n");
fs.mkdirSync(path.join(CWD, ".pi", "agents"), { recursive: true });
fs.writeFileSync(path.join(CWD, ".pi", "agents", "evil.md"), "---\nname: evil\ndescription: repo-controlled\n---\npwn\n");
process.on("exit", () => { fs.rmSync(AGENT, { recursive: true, force: true }); fs.rmSync(CWD, { recursive: true, force: true }); });

/** What runSingleAgent needs from a child: piped stdout/stderr (real streams, so decoding is the runtime's), close(code, sig). */
class FakeChild extends EventEmitter {
	stdout = new PassThrough();
	stderr = new PassThrough();
	pid = 4242;
	exitCode: number | null = null;
	signalCode: string | null = null;
	constructor(public args: string[]) { super(); }
	kill() { return true; }
	say(text: string, extra: Record<string, any> = {}) { this.stdout.write(JSON.stringify({ type: "message_end", message: { role: "assistant", content: [{ type: "text", text }], stopReason: "end", ...extra } }) + "\n"); }
	end(code: number | null = 0, sig: string | null = null) { this.exitCode = code; this.signalCode = sig; this.emit("close", code, sig); }
}
let spawns: FakeChild[] = [];
let nextSpawn: (args: string[]) => FakeChild = () => new FakeChild([]);
/** a child that runs script once started and ends by itself */
const child = (script: (c: FakeChild) => void) => () => { const c = new FakeChild([]); setImmediate(() => script(c)); return c; };
const e2big = () => Object.assign(new Error("spawn E2BIG"), { code: "E2BIG" });
mock.module("node:child_process", () => ({ spawn: (_cmd: string, args: string[]) => { const c = nextSpawn(args); c.args = args; spawns.push(c); return c; } }));
// just enough of pi for the tool to register and run: no UI classes, a frontmatter splitter, nothing else
mock.module("@earendil-works/pi-coding-agent", () => ({
	getAgentDir: () => AGENT, CONFIG_DIR_NAME: ".pi", getMarkdownTheme: () => ({}),
	parseFrontmatter: (s: string) => {
		const m = s.match(/^---\r?\n([\s\S]*?)\r?\n---\r?\n?([\s\S]*)$/);
		const fm = Object.fromEntries((m?.[1] ?? "").split(/\r?\n/).filter((l) => l.includes(":")).map((l) => { const i = l.indexOf(":"); return [l.slice(0, i).trim(), l.slice(i + 1).trim()]; }));
		return { frontmatter: fm, body: (m?.[2] ?? s).trim() };
	},
}));
mock.module("@earendil-works/pi-tui", () => ({ Container: class { addChild() {} }, Markdown: class {}, Spacer: class {}, Text: class { constructor(public text: string) {} } }));
mock.module("typebox", () => ({ Type: { Object: (o: any) => o, String: () => ({}), Array: () => ({}), Optional: (x: any) => x } }));
mock.module("@earendil-works/pi-ai", () => ({ StringEnum: () => ({}) }));
const { default: subagentTool } = await import("../extensions/subagent/index.ts");

let tool: any;
subagentTool({ registerTool: (t: any) => { tool = t; } } as any);
const ctx = { cwd: CWD, isProjectTrusted: () => false, hasUI: false } as any;
const run = (params: any, onUpdate?: (u: any) => void) => tool.execute("call", params, new AbortController().signal, onUpdate, ctx);
const text = (r: any) => r.content[0].text as string;

test("single: a child that cannot be spawned is a failed result, not a rejected call", async () => {
	nextSpawn = () => { throw e2big(); };
	const r = await run({ agent: "worker", task: "x".repeat(131072) });
	expect(r.isError).toBe(true);
	expect(text(r)).toContain("could not start the subagent: spawn E2BIG");
	expect(r.details.results[0].exitCode).toBe(1);
});

test("single: a child killed by a signal is reported as failed, with the signal, not as its last intermediate text", async () => {
	nextSpawn = child((c) => { c.say("I'll now edit foo.ts…"); c.end(null, "SIGKILL"); });
	const r = await run({ agent: "worker", task: "t" });
	expect(r.isError).toBe(true);
	expect(r.details.results[0].exitCode).toBe(1);
	expect(r.details.results[0].stderr).toContain("[subagent was killed (SIGKILL)]");
	expect(text(r)).toContain("killed (SIGKILL)");
});

test("a clean child returns its final text; tool results are not collected, only assistant messages", async () => {
	spawns = [];
	const ev = JSON.stringify({ type: "message_end", message: { role: "assistant", content: [{ type: "text", text: "final" }], stopReason: "end" } });
	nextSpawn = child((c) => {
		c.stdout.write(JSON.stringify({ type: "message_end", message: { role: "toolResult", content: [{ type: "text", text: "x".repeat(1000) }] } }) + "\n");
		c.stdout.write(ev.slice(0, 40)); // one event split over two chunks
		c.stdout.write(ev.slice(40) + "\n");
		c.end(0);
	});
	const r = await run({ agent: "worker", task: "t" });
	expect(r.isError).toBeUndefined();
	expect(text(r)).toBe("final");
	expect(r.details.results[0].messages.map((m: any) => m.role)).toEqual(["assistant"]);
	expect(r.details.results[0].usage.turns).toBe(1);
});

test("a multibyte character split across two stdout chunks arrives whole", async () => {
	nextSpawn = child((c) => {
		const line = Buffer.from(JSON.stringify({ type: "message_end", message: { role: "assistant", content: [{ type: "text", text: "中文答案" }], stopReason: "end" } }) + "\n", "utf8");
		const cut = line.indexOf(Buffer.from("中")) + 1; // inside the 3-byte character
		c.stdout.write(line.subarray(0, cut));
		setTimeout(() => { c.stdout.write(line.subarray(cut)); c.end(0); }, 5);
	});
	expect(text(await run({ agent: "worker", task: "t" }))).toBe("中文答案");
});

test("chain: {previous} carries the prior step's output as text; an LLM error stops the chain and renders as failed", async () => {
	spawns = [];
	nextSpawn = child((c) => { c.say(c === spawns[0] ? "a $& b {previous}" : "done"); c.end(0); });
	const r = await run({ chain: [{ agent: "worker", task: "step one" }, { agent: "worker", task: "review {previous} and {previous}" }] });
	expect(r.isError).toBeUndefined();
	expect(text(r)).toBe("done");
	expect(spawns[1].args.at(-1)).toBe("Task: review a $& b {previous} and a $& b {previous}");
	// pi exits 0 on an LLM error: the step is still a failure, in the result and in the collapsed render
	nextSpawn = child((c) => { c.say("partial", { stopReason: "error", errorMessage: "context overflow" }); c.end(0); });
	const bad = await run({ chain: [{ agent: "worker", task: "one" }, { agent: "worker", task: "two" }] });
	expect(bad.isError).toBe(true);
	expect(text(bad)).toBe("Chain stopped at step 1 (worker): context overflow");
	const theme = { fg: (_c: string, t: string) => t, bold: (t: string) => t } as any;
	const shown = tool.renderResult(bad, { expanded: false }, theme, ctx).text;
	expect(shown).toContain("0/1 steps");
	expect(shown).toContain("Step 1: worker ✗");
	expect(shown).not.toContain("✓");
});

test("parallel: a task that cannot start does not end the call or drop its siblings", async () => {
	spawns = [];
	// which worker spawns first is not fixed (both await their prompt file first): decide by the task, not by the call order
	nextSpawn = (args) => {
		if (args.at(-1) === "Task: a") throw e2big();
		const c = new FakeChild([]);
		setImmediate(() => { c.say(`ok ${args.at(-1).slice(-1)}`); c.end(0); });
		return c;
	};
	const updates: any[] = [];
	const r = await run({ tasks: [{ agent: "worker", task: "a" }, { agent: "worker", task: "b" }] }, (u) => updates.push(u));
	expect(r.isError).toBeUndefined();
	expect(text(r)).toContain("Parallel: 1/2 succeeded");
	expect(text(r)).toContain("spawn E2BIG");
	expect(text(r)).toContain("ok b");
	expect(r.details.results.map((x: any) => x.exitCode)).toEqual([1, 0]);
	expect(updates.at(-1).content[0].text).toBe("Parallel: 2/2 done, 0 running...");
});

test("parallel: at most 8 tasks, and a huge output is truncated in the summary but kept in details", async () => {
	spawns = [];
	const r = await run({ tasks: Array.from({ length: 9 }, (_, i) => ({ agent: "worker", task: `t${i}` })) });
	expect(text(r)).toContain("Too many parallel tasks (9). Max is 8.");
	expect(spawns).toHaveLength(0);
	nextSpawn = child((c) => { c.say("y".repeat(60 * 1024)); c.end(0); });
	const big = await run({ tasks: [{ agent: "worker", task: "big" }] });
	expect(text(big)).toContain("[Output truncated");
	expect(big.details.results[0].messages[0].content[0].text).toHaveLength(60 * 1024);
});

test("project agents are refused when the project is not trusted and there is no one to ask; unknown agents named", async () => {
	spawns = [];
	const r = await run({ agent: "evil", task: "t", agentScope: "both" });
	expect(text(r)).toContain("Refused: project-local agents");
	expect(spawns).toHaveLength(0);
	nextSpawn = child((c) => c.say("never runs"));
	expect(text(await run({ agent: "nobody", task: "t" }))).toContain('Unknown agent: "nobody". Available agents: "worker".');
	expect(spawns).toHaveLength(0);
	expect(text(await run({ agent: "worker" }))).toContain("Invalid parameters");
});
