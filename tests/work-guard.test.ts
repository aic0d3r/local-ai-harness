// run: bun test tests/   (guards bash from editing project source; scratch dirs and data text must pass)
import { expect, mock, test } from "bun:test";

mock.module("@earendil-works/pi-coding-agent", () => ({ getAgentDir: () => "/tmp" }));
const { blocksBashEdit } = await import("../extensions/work-guard.ts");

const PROJ = "/home/me/proj";

test("blocks source edits routed through bash", () => {
	expect(blocksBashEdit("sed -i s/a/b/ x.go", PROJ)).toBe(true);
	expect(blocksBashEdit("perl -pi -e 's/a/b/' x.go", PROJ)).toBe(true);
	expect(blocksBashEdit("cat > a.go <<'EOF'\npackage x\nEOF", PROJ)).toBe(true);
	expect(blocksBashEdit("echo x >> src/main.ts", PROJ)).toBe(true);
	expect(blocksBashEdit("tee out.py", PROJ)).toBe(true);
	expect(blocksBashEdit("python3 - <<'PY'\nopen('a.go','w').write(s)\nPY", PROJ)).toBe(true);
});

test("quoted text is data: grep/echo/commit messages that mention sed -i pass, a real sed -i still blocks", () => {
	expect(blocksBashEdit(`grep -n "sed -i" setup.sh`, PROJ)).toBe(false);
	expect(blocksBashEdit(`grep -n -e 'sed -i' -e "perl -pi" src/`, PROJ)).toBe(false);
	expect(blocksBashEdit(`git commit -m "stop sed -i in upgrade scripts"`, PROJ)).toBe(false);
	expect(blocksBashEdit(`echo 'use sed -i here'`, PROJ)).toBe(false);
	expect(blocksBashEdit(`sed -i 's/a/b/' x.go`, PROJ)).toBe(true);
	expect(blocksBashEdit(`sed -i "s/a/b/" x.go`, PROJ)).toBe(true);
	expect(blocksBashEdit(`bash -c "sed -i s/a/b/ x.go"`, PROJ)).toBe(true);
	expect(blocksBashEdit(`find . -name '*.go' | xargs sed -i 's/a/b/'`, PROJ)).toBe(true);
});

test("lets scratch work, reads and build/test commands through", () => {
	expect(blocksBashEdit("cd /tmp; python3 - <<'PY'\nopen('p.py','w').write(s)\nPY", PROJ)).toBe(false);
	expect(blocksBashEdit("cd /tmp && sed -i s/a/b/ scratch.py", PROJ)).toBe(false);
	expect(blocksBashEdit("sed -i s/a/b/ x.go", "/tmp")).toBe(false);
	expect(blocksBashEdit("cmd 2>&1 | tail -3", PROJ)).toBe(false);
	expect(blocksBashEdit("node -e 'if (s.length > 72) { throw 1 }' 2>&1 | tail -5", PROJ)).toBe(false);
	expect(blocksBashEdit("curl -s -d '{\"a\":1}' -o /dev/null url/v1/x", PROJ)).toBe(false);
	expect(blocksBashEdit("timeout 12 bash ~/x/scripts/halogen-top.sh 2 2>&1 | tail -3", PROJ)).toBe(false);
	expect(blocksBashEdit("go test ./...", PROJ)).toBe(false);
	expect(blocksBashEdit("sed -n 1,5p a.go", PROJ)).toBe(false);
});

test("the skill-first rule only applies where the skill is installed", async () => {
	const fs = await import("node:fs"), os = await import("node:os"), path = await import("node:path");
	const { skillInstalled } = await import("../extensions/work-guard.ts");
	const dir = fs.mkdtempSync(path.join(os.tmpdir(), "wg-"));
	expect(skillInstalled(dir)).toBe(false);
	fs.mkdirSync(path.join(dir, "skills", "ui-ux-work"), { recursive: true });
	fs.writeFileSync(path.join(dir, "skills", "ui-ux-work", "SKILL.md"), "x");
	expect(skillInstalled(dir)).toBe(true);
});

test("UI paths and the skill file match with Windows backslashes too", async () => {
	const { UI_PATH, SKILL } = await import("../extensions/work-guard.ts");
	for (const p of ["src/app.css", "src\\app.css", "C:\\p\\components\\x.ts", "/p/ui/x.ts"]) expect(UI_PATH.test(p)).toBe(true);
	for (const p of ["src/main.ts", "C:\\p\\src\\main.ts", "/p/guide/x.ts"]) expect(UI_PATH.test(p)).toBe(false);
	expect(SKILL.test("/h/.pi/agent/skills/ui-ux-work/SKILL.md")).toBe(true);
	expect(SKILL.test("C:\\Users\\me\\.pi\\agent\\skills\\ui-ux-work\\SKILL.md")).toBe(true);
});
