import { expect, test } from "bun:test";
import { spawn } from "node:child_process";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { otherOwner } from "../extensions/harness-status/session-lock.ts";

const lock = (pid: number | string) => {
	const f = join(mkdtempSync(join(tmpdir(), "lock-")), "s.jsonl.pid");
	writeFileSync(f, String(pid));
	return f;
};

test("no lock, our own pid, or a dead pid: nobody else", () => {
	expect(otherOwner("/nonexistent/lock")).toBeUndefined();
	expect(otherOwner(lock(process.pid))).toBeUndefined();
	expect(otherOwner(lock(2 ** 22 + 7))).toBeUndefined(); // above pid_max's default range: never alive
});

test("a live pid counts only when it is pi (a reused pid is not)", async () => {
	const other = spawn("sleep", ["30"]);
	const pi = spawn("sleep", ["30"], { argv0: "pi" });
	await new Promise((r) => setTimeout(r, 100));
	try {
		expect(otherOwner(lock(other.pid!))).toBeUndefined();
		expect(otherOwner(lock(pi.pid!))).toBe(pi.pid);
	} finally {
		other.kill();
		pi.kill();
	}
});
