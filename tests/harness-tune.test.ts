// run: bun test tests/   (/tune: reset follows the real context window, and only halogen's maxTokens cap; set validates its keys)
import { expect, mock, test } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

const agent = fs.mkdtempSync(path.join(os.tmpdir(), "tune-"));
mock.module("@earendil-works/pi-coding-agent", () => ({ getAgentDir: () => agent }));
process.on("exit", () => fs.rmSync(agent, { recursive: true, force: true }));

const SETTINGS = path.join(agent, "settings.json"), MODELS = path.join(agent, "models.json"), UBATCH = path.join(agent, "harness-ubatch");
/** an install for engine on a window of ctx, with a /tune-touched reserve, maxTokens and temperature */
const install = (engine: string, ctx: number) => {
	fs.writeFileSync(path.join(agent, "harness-profile.json"), JSON.stringify({ engine, provider: engine, model: "m", sidecarUrl: "http://127.0.0.1:8090", npu: engine === "halogen", npuUrl: "http://127.0.0.1:8731" }));
	fs.writeFileSync(MODELS, JSON.stringify({ providers: { [engine]: { baseUrl: "http://127.0.0.1:1/v1", models: [{ id: "m", contextWindow: ctx, maxTokens: 4096, samplingParams: { temperature: 0.3, top_p: 0.9, min_p: 0.05 } }] } } }));
	fs.writeFileSync(SETTINGS, JSON.stringify({ compaction: { enabled: true, reserveTokens: 5000 } }));
	fs.writeFileSync(UBATCH, "2048");
};
// harness-tune reads the profile at load: reinstall for each engine by re-importing with a fresh module URL
const ext = async (engine: string, ctx: number) => {
	install(engine, ctx);
	return (await import(`../extensions/harness-tune.ts?${engine}`)).default;
};
const tune = (pi: any) => {
	let cmd: any;
	pi({ registerCommand: (_n: string, c: any) => { cmd = c; }, on: () => {}, registerTool: () => {} } as any);
	const said: string[] = [];
	return { run: (...args: string[]) => cmd.handler(args.join(" "), { ui: { notify: (t: string) => said.push(t) } } as any), said };
};
const settings = () => JSON.parse(fs.readFileSync(SETTINGS, "utf8"));
const entry = () => JSON.parse(fs.readFileSync(MODELS, "utf8")).providers[Object.keys(JSON.parse(fs.readFileSync(MODELS, "utf8")).providers)[0]].models[0];

test("reset on a 1M-window osmos keeps the installer's reserve rule and the model's own maxTokens", async () => {
	const t = tune(await ext("osmos", 1048576));
	await t.run();
	expect(t.said[0]).toContain("defaults: compactAt=989, maxTokens=8192, temperature=1, ubatch=4096; maxTokens is halogen's cap");
	expect(t.said[0]).toContain("compactAt    1044"); // (1048576 - 5000) / 1000 before the reset
	await t.run("reset");
	// setup/harness.py reserve_for(1048576) = 60000: compaction at ~989k, not at halogen's 202k (the knob is whole k tokens:
	// the reserve lands within 1k of the rule's value)
	expect(settings().compaction.reserveTokens).toBe(59576);
	expect(entry().maxTokens).toBe(4096); // another engine's cap is its own
	expect(entry().samplingParams.temperature).toBe(1);
	expect(fs.readFileSync(UBATCH, "utf8")).toBe("4096");
	expect(t.said.at(-1)).toContain("reset to defaults: compactAt=989");
});

test("reset on halogen (262k) restores 202 and the 8192 cap; a 65k day clamps to 49", async () => {
	let t = tune(await ext("halogen", 262144));
	await t.run("reset");
	expect(settings().compaction.reserveTokens).toBe(60144);
	expect(entry().maxTokens).toBe(8192);
	expect(t.said.at(-1)).toBe("reset to defaults: compactAt=202, temperature=1, ubatch=4096, maxTokens=8192");
	t = tune(await ext("halogen", 65536));
	await t.run("reset");
	expect(settings().compaction.reserveTokens).toBe(16536);
	expect(t.said.at(-1)).toContain("compactAt=49");
});

test("set: every key is known, values are bounded, an unknown key is refused", async () => {
	const t = tune(await ext("osmos", 131072));
	await t.run("compactAt", "100");
	expect(settings().compaction.reserveTokens).toBe(31072);
	await t.run("compactAt", "999");
	expect(t.said.at(-1)).toContain("must be between 10 and 121");
	await t.run("nosuch", "1");
	expect(t.said.at(-1)).toContain("unknown key 'nosuch'. keys: compactAt, maxTokens, temperature, ubatch");
	await t.run("maxTokens", "2048");
	expect(entry().maxTokens).toBe(2048);
});
