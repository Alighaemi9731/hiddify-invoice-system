import { useState } from "react";
import { ThemeProvider } from "@mui/material";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { makeTheme } from "../theme";
import { NumberField, numberValue, toAsciiDigits } from "../components/NumberField";

/** A controlled host, exactly how every real call site uses the field. */
function Host({ initial = "" }: { initial?: string }) {
  const [value, setValue] = useState(initial);
  return (
    <ThemeProvider theme={makeTheme("light")}>
      <NumberField label="مبلغ" value={value} onChange={setValue} />
      <output data-testid="staged">{value}</output>
    </ThemeProvider>
  );
}

const field = () => screen.getByLabelText("مبلغ") as HTMLInputElement;
const staged = () => screen.getByTestId("staged").textContent;

describe("NumberField", () => {
  it("selects the whole value on click so the next digit replaces it", async () => {
    const user = userEvent.setup();
    render(<Host initial="2000" />);

    await user.click(field());
    expect(field().selectionStart).toBe(0);
    expect(field().selectionEnd).toBe(4);

    await user.keyboard("7");
    expect(field().value).toBe("7");
  });

  it("can be emptied and stays empty — the old value must not snap back", async () => {
    const user = userEvent.setup();
    render(<Host initial="2000" />);

    await user.clear(field());
    expect(field().value).toBe("");
    expect(staged()).toBe("");

    // ...and the caller parses that to null, never to 0.
    expect(numberValue("")).toBeNull();
  });

  it("accepts Persian digits and drops the separators a paste drags along", async () => {
    const user = userEvent.setup();
    render(<Host />);

    await user.type(field(), "۵۰۰۰۰");
    expect(field().value).toBe("50000");

    await user.clear(field());
    await user.paste("۱٬۲۳۴ تومان");
    expect(field().value).toBe("1234");
  });

  it("is an LTR text input, so an RTL page cannot reverse the caret or the wheel change it", () => {
    render(<Host initial="10" />);
    expect(field()).toHaveAttribute("type", "text");
    expect(field()).toHaveAttribute("dir", "ltr");
    expect(field()).toHaveAttribute("inputmode", "numeric");
  });

  it("keeps a second click free to place the caret for a one-digit fix", async () => {
    const user = userEvent.setup();
    render(<Host initial="1500" />);

    await user.click(field());          // focus → select all
    await user.click(field());          // already focused → normal caret placement
    expect(field().selectionStart).toBe(field().selectionEnd);
  });

  it("normalizes Persian and Arabic-Indic digits identically", () => {
    expect(toAsciiDigits("۱۲۳۴۵۶۷۸۹۰")).toBe("1234567890");
    expect(toAsciiDigits("١٢٣٤٥٦٧٨٩٠")).toBe("1234567890");
  });

  it("parses only what is really a number", () => {
    expect(numberValue("50000")).toBe(50000);
    expect(numberValue("  12  ")).toBe(12);
    expect(numberValue("")).toBeNull();
    expect(numberValue("-")).toBeNull();
    expect(numberValue(".")).toBeNull();
  });
});
