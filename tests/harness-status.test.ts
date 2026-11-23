// run: bun test tests/   (harness-status: the footer names the model pi selected; a pack osmos serves beside DeepSeek is not DeepSeek)
import { expect, mock, test } from "bun:test";

mock.module("@earendil-works/pi-coding-agent", () => ({ getAgentDir: () => "/tmp", FooterComponent: class { render() { return []; } } }));
mock.module("@earendil-works/pi-tui", () => ({ truncateToWidth: (s: string) => s }));
const { footerLabel } = await import("../extensions/harness-status/index.ts");

test("footer label: the engine's own model reads as its model_label + weights, a pack beside it as the name /model lists", () => {
	const osmos = { model_label: "DeepSeek-V4.1-Flash (osmos-evo)" };
	expect(footerLabel({ id: "osmos-osmos-evo", name: "DeepSeek-V4.1-Flash (osmos-evo)" }, osmos, "stock")).toBe("DeepSeek-V4.1-Flash (osmos-evo) · stock");
	expect(footerLabel({ id: "osmos-inkling-2-mini", name: "inkling-2-mini (osmos)" }, osmos, "stock")).toBe("inkling-2-mini (osmos)");
});
