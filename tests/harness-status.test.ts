// run: bun test tests/   (harness-status runs in its own process: the checks are in harness-status.check.ts)
import { expect, test } from "bun:test";

test("harness-status in its own process (mock.module is process-wide)", () => {
	// "./": bun treats a bare filter as a name pattern, and the check file is not called *.test.ts on purpose
	const p = Bun.spawnSync(["bun", "test", "./harness-status.check.ts"], { cwd: import.meta.dir, stdout: "pipe", stderr: "pipe" });
	const out = p.stdout.toString() + p.stderr.toString();
	if (p.exitCode !== 0) console.log(out);
	expect(p.exitCode).toBe(0);
	expect(out).toContain("1 pass");
}, 30_000);
