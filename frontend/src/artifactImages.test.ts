import { describe, expect, it } from "vitest";
import { artifactImages, splitArtifactImages } from "./artifactImages";

// Code scanning (2026-09-27): the matched URL went straight into href/src.
// It is rebuilt from the two validated parts, so nothing but our own image
// path can ever reach either attribute.
describe("artifactImages", () => {
  const id = "0123456789abcdef0123456789abcdef";
  it("rebuilds the URL from the project and the id", () => {
    const text = `look ![the shot](/api/artifacts/shop/${id}) done`;
    expect(artifactImages(text)).toEqual([{ caption: "the shot", url: `/api/artifacts/shop/${id}` }]);
    expect(splitArtifactImages(text)).toEqual([
      { kind: "text", text: "look " },
      { kind: "image", caption: "the shot", url: `/api/artifacts/shop/${id}` },
      { kind: "text", text: " done" },
    ]);
  });
  it("turns nothing else into an image", () => {
    for (const bad of ["![x](javascript:alert(1))", "![x](https://evil.test/a.png)", `![x](/api/artifacts/../${id})`,
                       `![x](/api/artifacts/shop/${id.slice(1)})`, "![x](/api/artifacts/shop/zz)"]) {
      expect(artifactImages(bad)).toEqual([]);
      expect(splitArtifactImages(bad)).toEqual([{ kind: "text", text: bad }]);
    }
  });
});
