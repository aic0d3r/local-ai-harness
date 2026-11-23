// run: bun test tests/   (subagent: what a project-local agent may not take from its own frontmatter, the exit-code and
// {previous} helpers, and the tool itself against a fake pi child in its own process: tests/subagent.check.ts)
import { expect, test } from "bun:test";
import { confine } from "../extensions/subagent/confine.ts";
import { exitCodeOf, killedNote, withPrevious } from "../extensions/subagent/result.ts";

const evil = { name: "evil", model: "some-cloud/model", skills: ["/etc/passwd", "~/.ssh/id_rsa", "../../x", "a/b", "..", ".", "good-skill", "also.ok_1"] };

test("a project agent cannot pick the model, and only bare skill names survive", () => {
	const a = confine("project", evil);
	expect(a.model).toBeUndefined();
	expect(a.skills).toEqual(["good-skill", "also.ok_1"]);
	expect(a.name).toBe("evil"); // everything else is untouched
});

test("a project agent with no usable skill ends up with none (not an empty list)", () => {
	expect(confine("project", { skills: ["/etc/shadow", "~/x"] }).skills).toBeUndefined();
	expect(confine("project", {}).skills).toBeUndefined();
});

test("a user agent keeps its model and its skill paths", () => {
	const a = confine("user", evil);
	expect(a.model).toBe("some-cloud/model");
	expect(a.skills).toEqual(evil.skills);
});

test("the input object is not modified", () => {
	const copy = JSON.stringify(evil);
	confine("project", evil);
	expect(JSON.stringify(evil)).toBe(copy);
});

test("exit code: a child killed by a signal is a failure (code is null, not 0); a timeout is 124", () => {
	expect(exitCodeOf(0, false)).toBe(0);
	expect(exitCodeOf(3, false)).toBe(3);
	expect(exitCodeOf(null, false)).toBe(1);
	expect(exitCodeOf(null, true)).toBe(124);
	expect(killedNote(null, "SIGKILL", false)).toBe("\n[subagent was killed (SIGKILL)]");
	expect(killedNote(0, null, false)).toBe("");
	expect(killedNote(null, "SIGTERM", true)).toBe(""); // the timeout note is already there
});

test("{previous}: every occurrence, and $& in the previous output stays text", () => {
	expect(withPrevious("review {previous}, then {previous} again", "a $& b `x`")).toBe("review a $& b `x`, then a $& b `x` again");
	expect(withPrevious("no placeholder", "x")).toBe("no placeholder");
});

test("the tool against a fake pi child (its own process: mock.module is process-wide)", async () => {
	// a child that cannot start, one killed by a signal, a multibyte split, {previous}, a failed chain step rendered as
	// failed, a rejected parallel task, the 8-task cap, output truncation, the project-agent trust gate
	// "./": bun treats a bare filter as a name pattern, and this file is not called *.test.ts on purpose
	const p = Bun.spawnSync(["bun", "test", "./subagent.check.ts"], { cwd: import.meta.dir, stdout: "pipe", stderr: "pipe" });
	const out = p.stdout.toString() + p.stderr.toString();
	if (p.exitCode !== 0) console.log(out);
	expect(p.exitCode).toBe(0);
	expect(out).toContain("8 pass");
}, 30_000);
