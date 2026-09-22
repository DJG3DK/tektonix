import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import { TektonixMark } from "./TektonixMark";

describe("<TektonixMark />", () => {
  it("draws the mark with no background of its own", () => {
    // The whole point: it floats. The favicon is a rounded tile with a dark
    // background baked in, which on a dark surface reads as a flat square.
    const { container } = render(<TektonixMark />);
    const svg = container.querySelector("svg")!;
    expect(svg.getAttribute("fill")).toBe("none");
    expect(container.querySelector("rect")).toBeNull();   // no plate
    expect(svg.querySelectorAll("path").length).toBe(2);  // the T, and the bob
  });

  it("keeps the brand colours rather than following the theme", () => {
    // --accent is per-theme (gold / periwinkle / orchid). Following it would
    // recolour the plumb bob to match whichever theme is on — cohesive, and
    // no longer the Tektonix mark. The sidebar lockup is fixed for the same
    // reason and sits on the same screen.
    const { container } = render(<TektonixMark />);
    const fills = [...container.querySelectorAll("path")].map((p) => p.getAttribute("fill"));
    expect(fills).toEqual(["#e8edf4", "#d4a72c"]);
    expect(fills.some((f) => f?.includes("var("))).toBe(false);
  });

  it("scales without resampling", () => {
    const { container } = render(<TektonixMark size={64} />);
    const svg = container.querySelector("svg")!;
    expect(svg.getAttribute("width")).toBe("64");
    // The viewBox is what makes it crisp at any size, including 32px on a
    // retina display, where a 32px PNG is not.
    expect(svg.getAttribute("viewBox")).toBe("0 0 24 24");
  });

  it("is hidden from screen readers", () => {
    // The speaker's name is already beside it in text; "Tektonix logo, Agent"
    // is worse than "Agent".
    const { container } = render(<TektonixMark />);
    expect(container.querySelector("svg")!.getAttribute("aria-hidden")).toBe("true");
  });
});
