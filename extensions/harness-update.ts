/**
 * harness-update: once-a-day check for a new harness release (vX.Y.Z tag the checkout
 * does not contain), and `/harness-update` to apply it. Never auto-applies: notice only;
 * the command is fast-forward-only, refuses on a dirty tree, and rolls back if the
 * refresh fails. Unreleased master commits are not offered. Repo path comes from the
 * harness-repo marker that setup.sh writes.
 */
import { execFile } from "node:child_process";
import * as fs from "node:fs";
import * as path from "node:path";
import { promisify } from "node:util";
import { type ExtensionAPI, getAgentDir } from "@earendil-works/pi-coding-agent";

const run = promisify(execFile);
const AGENT_DIR = getAgentDir();
// not "harness-update-check": that file holds setup.sh's halogen-update answer (1/0) and this stamp wiped it
const STAMP = path.join(AGENT_DIR, "harness-update-stamp");
const DAY = 24 * 3600 * 1000;

const git = (repo: string, ...a: string[]) => run("git", ["-C", repo, ...a], { timeout: 5000 }).then((r) => r.stdout.trim());
const repoPath = () => {
	try { return fs.readFileSync(path.join(AGENT_DIR, "harness-repo"), "utf8").trim() || null; } catch { return null; }
};
// newest release tag this checkout does not contain yet (n = commits it adds), or null
type Release = { tag: string; n: number } | null;
const newRelease = async (repo: string): Promise<Release> => {
	await git(repo, "fetch", "--quiet", "--tags");
	const tag = (await git(repo, "tag", "--list", "v*", "--sort=-v:refname")).split("\n")[0];
	if (!tag) return null;
	const n = Number(await git(repo, "rev-list", "--count", `HEAD..${tag}`));
	return n > 0 ? { tag, n } : null;
};

// Windows installs with setup.ps1 (adopts the running engine). Linux runs `harness refresh`, which re-installs for the
// engine the wizard saved; a bare setup.sh with no terminal would assume halogen and wire its NPU extensions onto any box.
export const installer = (repo: string, platform: string = process.platform): [string, string[]] =>
	platform === "win32"
		? ["powershell", ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", path.join(repo, "setup.ps1")]]
		: ["bash", [path.join(repo, "harness"), "refresh"]];

const why = (e: any) => String(e?.stderr || e?.message || e).slice(0, 300);
// ff-only to the release tag + installer; throws on a dirty tree. A failed refresh resets to the old commit and re-installs it.
const apply = async (repo: string, { tag, n }: { tag: string; n: number }) => {
	if (await git(repo, "status", "--porcelain")) throw new Error(`${repo} has uncommitted changes: commit or stash, then retry`);
	const old = await git(repo, "rev-parse", "HEAD");
	await run("git", ["-C", repo, "merge", "--ff-only", "--quiet", tag], { timeout: 60000 });
	const [cmd, args] = installer(repo);
	try {
		await run(cmd, args, { timeout: 300000 });
	} catch (e) {
		await git(repo, "reset", "--hard", "--quiet", old); // tree was clean before the merge: nothing of the user's is lost
		await run(cmd, args, { timeout: 300000 }).catch(() => {});
		throw new Error(`refresh failed on ${tag}, rolled back to ${old.slice(0, 7)}: ${why(e)}`);
	}
	return `updated to ${tag} (${n} commit${n > 1 ? "s" : ""}) and refreshed the install. Restart pi to load changed extensions.`;
};
const fail = (e: any) => `harness-update failed: ${why(e)}`;

export default function (pi: ExtensionAPI) {
	pi.on("session_start", async (_e, ctx) => {
		const repo = repoPath();
		if (!repo || process.env.HARNESS_NO_UPDATE_CHECK === "1") return; // opt-out: no daily git fetch
		try {
			if (Date.now() - fs.statSync(STAMP).mtimeMs < DAY) return;
		} catch { /* no stamp yet */ }
		try {
			const rel = await newRelease(repo);
			fs.writeFileSync(STAMP, "");
			if (!rel) return;
			// opt-in only: HARNESS_AUTOUPDATE=1 applies instead of just notifying
			if (process.env.HARNESS_AUTOUPDATE !== "1") return ctx.ui.notify(`harness ${rel.tag} is out: run /harness-update`, "info");
			try { ctx.ui.notify(await apply(repo, rel), "info"); } catch (e) { ctx.ui.notify(fail(e), "error"); }
		} catch { /* offline, no upstream, not a repo: stay silent, retry next start */ }
	});

	pi.registerCommand("harness-update", {
		description: "Update the harness repo to the latest release and refresh the install for your saved engine",
		handler: async (_args, ctx) => {
			const repo = repoPath();
			if (!repo) return ctx.ui.notify("no harness-repo marker: run ./setup.sh once from the repo", "error");
			try {
				const rel = await newRelease(repo);
				ctx.ui.notify(rel ? await apply(repo, rel) : "harness already on the latest release", "info");
			} catch (e) {
				ctx.ui.notify(fail(e), "error");
			}
		},
	});
}
