/**
 * What a project-local agent (a file in the repo's .pi/agents) may not take from its own frontmatter. Repo content is
 * written by someone else: it must not pick the model (its text could go to any configured cloud provider) nor point
 * `skills:` outside <agentDir>/skills (an absolute, ~/ or ../ path is read into its system prompt, and the child can
 * echo it back). User agents are the user's own and keep both. No pi imports: unit-tested on its own.
 */
export function confine<T extends { model?: string; skills?: string[] }>(source: "user" | "project", a: T): T {
	if (source === "user") return a;
	const skills = a.skills?.filter((s) => /^[\w.-]+$/.test(s) && s !== "." && s !== "..");
	return { ...a, model: undefined, skills: skills?.length ? skills : undefined };
}
