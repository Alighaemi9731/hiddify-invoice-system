import { forwardRef, useRef, type MouseEvent } from "react";
import { TextField, type TextFieldProps } from "@mui/material";

// Persian (۰-۹) and Arabic-Indic (٠-٩) digits, plus the separators an Iranian keyboard or a
// copy-pasted amount drags along («۵۰٬۰۰۰», "50,000"). The bot has normalized these since day one
// (`app/bot/storefront/handlers.py` `_digits`); the panel used to reject them outright.
const PERSIAN_ZERO = 0x06f0;
const ARABIC_ZERO = 0x0660;

export function toAsciiDigits(value: string): string {
  let out = "";
  for (const char of value) {
    const code = char.codePointAt(0) ?? 0;
    if (code >= PERSIAN_ZERO && code <= PERSIAN_ZERO + 9) out += String(code - PERSIAN_ZERO);
    else if (code >= ARABIC_ZERO && code <= ARABIC_ZERO + 9) out += String(code - ARABIC_ZERO);
    else out += char;
  }
  return out;
}

/** Keep only what can belong to a number, so a pasted «۵۰٬۰۰۰ تومان» lands as `50000`. */
function sanitize(raw: string, { decimal, negative }: { decimal: boolean; negative: boolean }) {
  const ascii = toAsciiDigits(raw).replace(/[٫٬،,\s]/g, "");
  const sign = negative && ascii.trimStart().startsWith("-") ? "-" : "";
  const body = ascii.replace(/[^0-9.]/g, "");
  if (!decimal) return sign + body.replace(/\./g, "");
  const [head, ...rest] = body.split(".");
  return sign + (rest.length ? `${head}.${rest.join("")}` : head);
}

export type NumberFieldProps = Omit<TextFieldProps, "value" | "onChange" | "type"> & {
  /** Raw text, NOT a number — an emptied field must be able to stay empty while it is edited. */
  value: string;
  /** Receives the sanitized text (ASCII digits only); it is never `undefined`. */
  onChange: (value: string) => void;
  allowDecimal?: boolean;
  allowNegative?: boolean;
};

/**
 * The one numeric input for the whole panel (DESIGN_SYSTEM.md §4.3a).
 *
 * Three things it fixes, all of which made numeric fields painful to edit in an RTL page:
 *
 * 1. **`type="text"`, not `type="number"`.** A number input inside `dir="rtl"` puts the caret on
 *    the wrong side and reverses what Backspace deletes; it also reports `""` for half-typed
 *    values and changes on scroll-wheel. Digits are constrained by `sanitize` instead, which is
 *    strictly stricter (it also strips a pasted «تومان»).
 * 2. **Text in, text out.** Callers that stored a `number` and did `Number(event.target.value)`
 *    turned an emptied field back into `0` on the next render (or, in Settings, discarded the
 *    keystroke and restored the old number) — so the field could not be cleared at all. Parsing
 *    belongs at submit time, not on every keystroke.
 * 3. **A click selects the whole value.** Tapping a field that already holds a number means
 *    "replace this", so the existing content is selected and the next digit overwrites it —
 *    no manual erasing first.
 */
export const NumberField = forwardRef<HTMLDivElement, NumberFieldProps>(function NumberField(
  { value, onChange, allowDecimal = false, allowNegative = false, inputProps, ...rest },
  ref,
) {
  // A mouse click fires focus (where we select) and THEN a mouseup that would collapse that
  // selection to a caret. Suppress only that first mouseup, so a second click still positions
  // the caret normally for someone who wants to edit one digit.
  const justFocused = useRef(false);

  return (
    <TextField
      {...rest}
      ref={ref}
      type="text"
      value={value}
      inputProps={{
        dir: "ltr",
        inputMode: allowDecimal ? "decimal" : "numeric",
        autoComplete: "off",
        // On the <input> itself, not the TextField root: the default action being suppressed here
        // is the input's own selection collapse, so the handler must sit on the element that owns it.
        onMouseUp: (event: MouseEvent<HTMLInputElement>) => {
          if (justFocused.current) event.preventDefault();
          justFocused.current = false;
        },
        ...inputProps,
      }}
      onFocus={(event) => {
        justFocused.current = true;
        event.target.select();
        rest.onFocus?.(event);
      }}
      onBlur={(event) => {
        justFocused.current = false;
        rest.onBlur?.(event);
      }}
      onChange={(event) =>
        onChange(sanitize(event.target.value, { decimal: allowDecimal, negative: allowNegative }))}
    />
  );
});

/** Parse a NumberField value for submission. Empty (or unparseable) yields `null`, never `0`. */
export function numberValue(raw: string): number | null {
  const trimmed = raw.trim();
  if (!trimmed || trimmed === "-" || trimmed === ".") return null;
  const parsed = Number(trimmed);
  return Number.isFinite(parsed) ? parsed : null;
}

export default NumberField;
