// run: bun test tests/   (temp origin + clone; no network, no real harness touched)
import { expect, mock, test } from "bun:test";
import { execFileSync } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "hu-"));
const agent = path.join(tmp, "agent");
const origin = path.join(tmp, "origin.git");
const work = path.join(tmp, "work");
const clone = path.join(tmp, "clone");
fs.mkdirSync(agent);
const sh = (cwd: string, ...a: string[]) => execFileSync("git", a, { cwd, stdio: "pipe" }).toString();
const commit = (msg: string) => {
	fs.writeFileSync(path.join(work, "f"), msg);
	sh(work, "add", "-A");
	sh(work, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", msg);
	sh(work, "push", "-q", "origin", "HEAD");
};
const release = (msg: string, tag: string) => {
	commit(msg);
	sh(work, "tag", tag);
	sh(work, "push", "-q", "origin", tag);
};
sh(tmp, "init", "-q", "--bare", "-b", "main", origin);
sh(tmp, "clone", "-q", origin, work);
// stands in for `harness refresh`: fails on the "bad" release, so the rollback has something to undo
fs.writeFileSync(path.join(work, "harness"), "#!/bin/sh\nd=\"$(dirname \"$0\")\"; touch \"$d/setup-ran\"; ! grep -qx bad \"$d/f\"\n");
release("one", "v1.0.0");
sh(tmp, "clone", "-q", origin, clone);
fs.writeFileSync(path.join(agent, "harness-repo"), clone + "\n");

mock.module("@earendil-works/pi-coding-agent", () => ({ getAgentDir: () => agent }));
const { default: ext } = await import("../extensions/harness-update.ts");

const handlers: Record<string, any> = {};
const cmds: Record<string, any> = {};
ext({ on: (n: string, h: any) => (handlers[n] = h), registerCommand: (n: string, o: any) => (cmds[n] = o.handler) } as any);
const notes: string[] = [];
const ctx = { ui: { notify: (m: string) => notes.push(m) } };
const stamp = path.join(agent, "harness-update-stamp");

test("quiet when up to date, writes stamp", async () => {
	await handlers.session_start({}, ctx);
	expect(notes).toEqual([]);
	expect(fs.existsSync(stamp)).toBe(true);
});

test("unreleased master commits are not offered", async () => {
	commit("wip");
	fs.utimesSync(stamp, 0, 0);
	await handlers.session_start({}, ctx);
	expect(notes).toEqual([]);
});

test("notice when a new release is out (stamp stale), no pull", async () => {
	release("two", "v1.1.0");
	fs.utimesSync(stamp, 0, 0);
	await handlers.session_start({}, ctx);
	expect(notes.at(-1)).toContain("harness v1.1.0 is out");
	expect(fs.existsSync(path.join(clone, "setup-ran"))).toBe(false);
});

test("fresh stamp skips the check", async () => {
	notes.length = 0;
	await handlers.session_start({}, ctx);
	expect(notes).toEqual([]);
});

test("dirty tree refuses", async () => {
	fs.writeFileSync(path.join(clone, "dirty"), "x");
	await cmds["harness-update"]("", ctx);
	expect(notes.at(-1)).toContain("uncommitted");
	fs.rmSync(path.join(clone, "dirty"));
});

test("command fast-forwards to the tag and runs `harness refresh`", async () => {
	await cmds["harness-update"]("", ctx);
	expect(notes.at(-1)).toContain("updated to v1.1.0 (2 commits)");
	expect(fs.readFileSync(path.join(clone, "f"), "utf8")).toBe("two");
	expect(fs.existsSync(path.join(clone, "setup-ran"))).toBe(true);
	fs.rmSync(path.join(clone, "setup-ran"));
	await cmds["harness-update"]("", ctx);
	expect(notes.at(-1)).toContain("latest release");
});

test("failed refresh rolls back to the old commit and re-installs it", async () => {
	const before = sh(clone, "rev-parse", "HEAD");
	release("bad", "v1.2.0");
	await cmds["harness-update"]("", ctx);
	expect(notes.at(-1)).toContain("refresh failed on v1.2.0, rolled back to");
	expect(sh(clone, "rev-parse", "HEAD")).toBe(before);
	expect(fs.readFileSync(path.join(clone, "f"), "utf8")).toBe("two");
	fs.rmSync(path.join(clone, "setup-ran"));
	expect(sh(clone, "status", "--porcelain")).toBe("");
});

test("HARNESS_AUTOUPDATE=1 applies at startup", async () => {
	release("three", "v1.3.0");
	fs.utimesSync(stamp, 0, 0);
	process.env.HARNESS_AUTOUPDATE = "1";
	await handlers.session_start({}, ctx);
	delete process.env.HARNESS_AUTOUPDATE;
	expect(notes.at(-1)).toContain("updated to v1.3.0");
	expect(fs.readFileSync(path.join(clone, "f"), "utf8")).toBe("three");
});

test("HARNESS_NO_UPDATE_CHECK=1: no daily fetch, no stamp", async () => {
	fs.rmSync(stamp, { force: true });
	notes.length = 0;
	process.env.HARNESS_NO_UPDATE_CHECK = "1";
	await handlers.session_start({}, ctx);
	delete process.env.HARNESS_NO_UPDATE_CHECK;
	expect(notes).toEqual([]);
	expect(fs.existsSync(stamp)).toBe(false);
});

test("Windows re-installs with setup.ps1, everything else with `harness refresh` (never a bare setup.sh)", async () => {
	const { installer } = await import("../extensions/harness-update.ts");
	expect(installer("/r", "linux")).toEqual(["bash", [path.join("/r", "harness"), "refresh"]]);
	const [cmd, args] = installer("/r", "win32");
	expect(cmd).toBe("powershell");
	expect(args.slice(-2)).toEqual(["-File", path.join("/r", "setup.ps1")]);
});
