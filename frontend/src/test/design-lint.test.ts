import { describe, expect, it } from "vitest";
// eslint-disable-next-line @typescript-eslint/ban-ts-comment
// @ts-ignore — plain .mjs module, no types
import { lintText, lintTree } from "../../scripts/design-lint.mjs";

// DS09: the drift guardrail must (a) pass on the standardized tree and (b) flag any
// non-canonical value loudly. These tests are the lint's own regression net.
describe("design lint", () => {
  it("flags a non-canonical hex, rgba, blur and radius", () => {
    const bad = [
      'const a = { color: "#123456" };',
      'const b = { background: "rgba(1,2,3,0.5)" };',
      'const c = { backdropFilter: "blur(33px) saturate(150%)" };',
      "const d = { borderRadius: 17 };",
    ].join("\n");
    const findings = lintText("src/pages/Fake.tsx", bad);
    const cats = findings.map((f) => f.category).sort();
    expect(cats).toEqual(["blur", "hex", "radius", "rgba"]);
  });

  it("accepts canonical values", () => {
    const good = [
      'const a = { color: "#0071e3", bg: "rgba(28,28,30,0.90)" };',
      'const b = { backdropFilter: "blur(40px) saturate(180%)" };',
      "const c = { borderRadius: 980, fontWeight: 750, fontSize: 12.5, zIndex: 3 };",
    ].join("\n");
    expect(lintText("src/pages/Fake.tsx", good)).toEqual([]);
  });

  it("passes on the real standardized tree", () => {
    const findings = lintTree();
    expect(findings).toEqual([]);
  });
});

describe("numeric inputs (§4.3a)", () => {
  it("flags a raw type=\"number\" anywhere but NumberField itself", () => {
    const offending = lintText("src/pages/Thing.tsx", '<TextField type="number" value={x} />');
    expect(offending.map((f) => f.category)).toContain("numberInput");
    // NumberField itself is the one module allowed to mention it (it renders type="text").
    expect(lintText("src/components/NumberField.tsx", 'const banned = \'type="number"\';').length).toBe(0);
  });
});
