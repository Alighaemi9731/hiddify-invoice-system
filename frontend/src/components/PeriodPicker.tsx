import { MenuItem, Stack, TextField } from "@mui/material";

// Billing periods are GREGORIAN months (the value is "YYYY-MM"); these are the
// Gregorian month names in Persian, shown with their number for clarity.
const MONTHS_FA = [
  "ژانویه", "فوریه", "مارس", "آوریل", "مه", "ژوئن",
  "ژوئیه", "اوت", "سپتامبر", "اکتبر", "نوامبر", "دسامبر",
];

const toFa = (n: number | string) => String(n).replace(/\d/g, (d) => "۰۱۲۳۴۵۶۷۸۹"[+d]);

// Years offered in the dropdown: a generous window around the current year so the
// owner can always reach past periods and the next one. Newest first.
function yearOptions(): number[] {
  const now = new Date().getFullYear();
  const start = Math.min(2023, now - 1);
  const end = now + 1;
  const out: number[] = [];
  for (let y = end; y >= start; y--) out.push(y);
  return out;
}

type Props = {
  value: string;                 // "YYYY-MM", or "" when allowEmpty
  onChange: (v: string) => void;
  label?: string;                // label for the year field (e.g. "دوره")
  allowEmpty?: boolean;          // adds a "همه" option that clears to ""
  size?: "small" | "medium";
};

/**
 * Two clear dropdowns (year + Persian month) instead of a native month input,
 * so changing the year is obvious. Emits a "YYYY-MM" string (or "" if allowEmpty
 * and either field is set to "همه").
 */
export default function PeriodPicker({
  value, onChange, label = "دوره", allowEmpty = false, size = "small",
}: Props) {
  const [yStr, mStr] = (value || "").split("-");
  const year = yStr ? Number(yStr) : "";
  const month = mStr ? Number(mStr) : "";

  const emit = (y: number | "", m: number | "") => {
    if (y === "" || m === "") {
      onChange("");                 // only valid when allowEmpty; clears the filter
      return;
    }
    onChange(`${y}-${String(m).padStart(2, "0")}`);
  };

  const years = yearOptions();
  if (year !== "" && !years.includes(year as number)) years.unshift(year as number);

  return (
    <Stack direction="row" spacing={1}>
      <TextField
        select size={size} label={label} value={year}
        sx={{ minWidth: 110 }}
        onChange={(e) => emit(e.target.value === "" ? "" : Number(e.target.value), allowEmpty && e.target.value === "" ? "" : (month || new Date().getMonth() + 1))}
      >
        {allowEmpty && <MenuItem value="">همه</MenuItem>}
        {years.map((y) => <MenuItem key={y} value={y}>{toFa(y)}</MenuItem>)}
      </TextField>
      <TextField
        select size={size} label="ماه" value={month}
        sx={{ minWidth: 130 }}
        onChange={(e) => emit(allowEmpty && e.target.value === "" ? "" : (year || new Date().getFullYear()), e.target.value === "" ? "" : Number(e.target.value))}
      >
        {allowEmpty && <MenuItem value="">همه</MenuItem>}
        {MONTHS_FA.map((name, i) => (
          <MenuItem key={i + 1} value={i + 1}>{`${name} (${toFa(String(i + 1).padStart(2, "0"))})`}</MenuItem>
        ))}
      </TextField>
    </Stack>
  );
}
