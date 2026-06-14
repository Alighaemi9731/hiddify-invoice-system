import { Select, MenuItem, Stack } from "@mui/material";
import { SelectChangeEvent } from "@mui/material/Select";

// Billing periods are GREGORIAN months (the value is "YYYY-MM"). Short English month
// names look cleaner than localized ones; the number keeps them unambiguous.
const MONTHS = [
  "Jan", "Feb", "Mar", "Apr", "May", "Jun",
  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
];

function yearOptions(): number[] {
  const now = new Date().getFullYear();
  const start = Math.min(2023, now - 1);
  const end = now + 1;
  const out: number[] = [];
  for (let y = end; y >= start; y--) out.push(y);
  return out;
}

type Props = {
  value: string;
  onChange: (v: string) => void;
  label?: string;
  allowEmpty?: boolean;
};

const selectSx = {
  "& .MuiOutlinedInput-notchedOutline": { borderRadius: "inherit" },
  "& .MuiSelect-select": { py: "7px !important" },
};

export default function PeriodPicker({ value, onChange, allowEmpty = false }: Props) {
  const [yStr, mStr] = (value || "").split("-");
  const year  = yStr ? Number(yStr) : ("" as const);
  const month = mStr ? Number(mStr) : ("" as const);

  const emit = (y: number | "", m: number | "") => {
    if (y === "" || m === "") { onChange(""); return; }
    onChange(`${y}-${String(m).padStart(2, "0")}`);
  };

  const years = yearOptions();
  if (year !== "" && !years.includes(year as number)) years.unshift(year as number);

  const handleYear = (e: SelectChangeEvent<number | "">) => {
    const v = e.target.value;
    if (v === "") { emit("", ""); return; }
    emit(Number(v), month !== "" ? month : new Date().getMonth() + 1);
  };

  const handleMonth = (e: SelectChangeEvent<number | "">) => {
    const v = e.target.value;
    if (v === "") { emit("", ""); return; }
    emit(year !== "" ? year : new Date().getFullYear(), Number(v));
  };

  return (
    <Stack direction="row" spacing={1}>
      <Select<number | "">
        size="small"
        value={year}
        displayEmpty
        onChange={handleYear}
        renderValue={(v) => v !== "" ? String(v) : "سال"}
        sx={{ minWidth: 88, ...selectSx }}
      >
        {allowEmpty && <MenuItem value="">همه</MenuItem>}
        {years.map((y) => <MenuItem key={y} value={y}>{y}</MenuItem>)}
      </Select>

      <Select<number | "">
        size="small"
        value={month}
        displayEmpty
        onChange={handleMonth}
        renderValue={(v) =>
          v !== ""
            ? `${String(Number(v)).padStart(2, "0")} · ${MONTHS[Number(v) - 1]}`
            : "ماه"
        }
        sx={{ minWidth: 108, ...selectSx }}
      >
        {allowEmpty && <MenuItem value="">همه</MenuItem>}
        {MONTHS.map((name, i) => (
          <MenuItem key={i + 1} value={i + 1}>
            {`${String(i + 1).padStart(2, "0")} · ${name}`}
          </MenuItem>
        ))}
      </Select>
    </Stack>
  );
}
