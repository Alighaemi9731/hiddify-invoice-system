import { MenuItem, Stack, TextField } from "@mui/material";

// Billing periods are GREGORIAN months (the value is "YYYY-MM"). Short English month
// names look cleaner than localized ones; the number keeps them unambiguous.
const MONTHS = [
  "Jan", "Feb", "Mar", "Apr", "May", "Jun",
  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
];

// Years offered: a window around the current year (newest first) so any past period and
// the next one are reachable.
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
  label?: string;                // label for the year field
  allowEmpty?: boolean;          // adds an "all" option that clears to ""
};

/**
 * Two compact dropdowns (year + month) instead of a native month input, so changing the
 * year is obvious. Emits a "YYYY-MM" string (or "" if allowEmpty and a field is cleared).
 */
export default function PeriodPicker({ value, onChange, label = "دوره", allowEmpty = false }: Props) {
  const [yStr, mStr] = (value || "").split("-");
  const year = yStr ? Number(yStr) : "";
  const month = mStr ? Number(mStr) : "";

  const emit = (y: number | "", m: number | "") => {
    if (y === "" || m === "") { onChange(""); return; }   // only reachable when allowEmpty
    onChange(`${y}-${String(m).padStart(2, "0")}`);
  };

  const years = yearOptions();
  if (year !== "" && !years.includes(year as number)) years.unshift(year as number);

  return (
    <Stack direction="row" spacing={1}>
      <TextField
        select size="small" label={label} value={year}
        sx={{ width: 96 }}
        onChange={(e) => emit(
          e.target.value === "" ? "" : Number(e.target.value),
          e.target.value === "" ? "" : (month || new Date().getMonth() + 1),
        )}
      >
        {allowEmpty && <MenuItem value="">همه</MenuItem>}
        {years.map((y) => <MenuItem key={y} value={y}>{y}</MenuItem>)}
      </TextField>
      <TextField
        select size="small" label="ماه" value={month}
        sx={{ width: 110 }}
        onChange={(e) => emit(
          e.target.value === "" ? "" : (year || new Date().getFullYear()),
          e.target.value === "" ? "" : Number(e.target.value),
        )}
      >
        {allowEmpty && <MenuItem value="">همه</MenuItem>}
        {MONTHS.map((name, i) => (
          <MenuItem key={i + 1} value={i + 1}>{`${String(i + 1).padStart(2, "0")} · ${name}`}</MenuItem>
        ))}
      </TextField>
    </Stack>
  );
}
