// session-lock: which other live pi process holds a session's lock file (no pi imports, so tests can load it)
import * as fs from "node:fs";

// pid of another live pi process that holds this session's lock, else undefined (dead, reused by a non-pi pid, or us)
export function otherOwner(lock: string, self = process.pid): number | undefined {
	let pid: number;
	try { pid = Number(fs.readFileSync(lock, "utf8").trim()); } catch { return undefined; }
	if (!pid || pid === self) return undefined;
	try { process.kill(pid, 0); } catch (e: any) { if (e.code !== "EPERM") return undefined; }
	try { return /(^|\/)pi\0|pi-coding-agent/.test(fs.readFileSync(`/proc/${pid}/cmdline`, "utf8")) ? pid : undefined; } catch { return pid; }
}
