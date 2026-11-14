// run: bun test tests/   (subagent: what a project-local agent may not take from its own frontmatter)
import { expect, test } from "bun:test";
import { confine } from "../extensions/subagent/confine.ts";

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
