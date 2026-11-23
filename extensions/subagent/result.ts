/**
 * Pure helpers of the subagent tool (no pi imports: unit-tested on their own, as confine.ts is).
 */

/** The exit code the parent reports for a child pi that ended with `code` (null when a signal killed it). 124 = timed out. */
export const exitCodeOf = (code: number | null, timedOut: boolean): number => (timedOut ? 124 : (code ?? 1));

/** What goes into stderr when the child died without an exit code (the OOM killer, a crash): it must not read as a clean run. */
export const killedNote = (code: number | null, sig: string | null, timedOut: boolean): string =>
	!timedOut && code === null ? `\n[subagent was killed (${sig})]` : "";

/** A chain step's task with every {previous} replaced by the prior step's output (a function: `$&` in the output stays text). */
export const withPrevious = (task: string, previous: string): string => task.replace(/\{previous\}/g, () => previous);
