import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { ThemeProvider } from "@mui/material/styles";
import { makeTheme } from "../theme";
import CapacityBar from "../components/CapacityBar";

// C12/DS07: LinearProgress-based bars take their 6px/4px geometry from the theme —
// no per-component height/borderRadius overrides.
describe("CapacityBar uses the theme progress geometry", () => {
  it("renders the theme's 6px height and 4px radius", () => {
    render(
      <ThemeProvider theme={makeTheme("light")}>
        <CapacityBar used={5} max={10} />
      </ThemeProvider>,
    );
    const bar = screen.getByRole("progressbar");
    expect(bar).toHaveStyle({ height: "6px", borderRadius: "4px" });
  });
});
