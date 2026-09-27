import { describe, expect, it } from "vitest";
import { safeHref } from "./safeHref";

describe("safeHref", () => {
  it.each([
    ["https://github.com/o/r/pull/9", "https://github.com/o/r/pull/9"],
    ["http://example.com/x", "http://example.com/x"],
    ["javascript:alert(document.cookie)", undefined],
    [" JavaScript:alert(1)", undefined],
    ["data:text/html,<script>1</script>", undefined],
    ["/relative/path", undefined],
    ["", undefined],
    [null, undefined],
    [undefined, undefined],
  ])("%s -> %s", (raw, expected) => {
    expect(safeHref(raw)).toBe(expected);
  });
});
