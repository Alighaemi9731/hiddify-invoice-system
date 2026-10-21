import { Stack, TextField, MenuItem, Checkbox, FormControlLabel, Typography, Button } from "@mui/material";
import { NumberField } from "../../components/NumberField";
import type { BroadcastFilters } from "../../api/client";

export default function SegmentFilters({ filters, periods, panels, onChange }: {
  filters: BroadcastFilters; periods: string[]; panels: { id: number; key: string }[];
  onChange: (value: BroadcastFilters) => void;
}) {
  const update = (patch: Partial<BroadcastFilters>) => onChange({ ...filters, ...patch });
  return <Stack spacing={2} sx={{ mb: 2 }}>
    <Stack direction={{ xs: "column", sm: "row" }} spacing={2} useFlexGap flexWrap="wrap">
      <TextField select size="small" label="پنل‌ها" sx={{ minWidth: 180 }}
        value={filters.panel_ids || []}
        SelectProps={{ multiple: true, displayEmpty: true,
          renderValue: (ids) => (ids as number[]).length
            ? panels.filter(p => (ids as number[]).includes(p.id)).map(p => p.key).join("، ") : "همهٔ پنل‌ها" }}
        InputLabelProps={{ shrink: true }}
        onChange={e => update({ panel_ids: e.target.value as unknown as number[] })}>
        {panels.map(p => <MenuItem key={p.id} value={p.id}>
          <Checkbox checked={filters.panel_ids?.includes(p.id) || false} size="small" />{p.key}
        </MenuItem>)}
      </TextField>
      {(["period_from", "period_to"] as const).map((key, i) => <TextField key={key} select size="small"
        label={i ? "تا ماه فاکتور" : "از ماه فاکتور"} sx={{ minWidth: 160 }} value={filters[key] || ""}
        onChange={e => update({ [key]: e.target.value || undefined })}>
        <MenuItem value="">همهٔ ماه‌ها</MenuItem>
        {[...new Set([...periods, filters[key]].filter(Boolean) as string[])].sort().reverse().map(p => <MenuItem key={p} value={p}>{p}</MenuItem>)}
      </TextField>)}
      <TextField select size="small" label="وضعیت دسترسی" sx={{ minWidth: 160 }} value={filters.access_state || ""}
        onChange={e => update({ access_state: (e.target.value || undefined) as BroadcastFilters["access_state"] })}>
        <MenuItem value="">همه</MenuItem><MenuItem value="active">فعال</MenuItem>
        <MenuItem value="frozen">فریز</MenuItem><MenuItem value="enforced">مسدود</MenuItem>
      </TextField>
    </Stack>
    <Typography variant="caption" color="text.secondary">
      دوره‌ها ماه میلادیِ فاکتور هستند. برای یک ماه، ابتدا و انتها را یکسان انتخاب کنید.
      مسدودهای یک دوره بر اساس اقدام مسدودیِ مرتبط با فاکتور همان دوره انتخاب می‌شوند؛ مسدودی دستی در این گروه نیست.
    </Typography>
    <Stack direction={{ xs: "column", sm: "row" }} spacing={2} useFlexGap flexWrap="wrap">
      {([ ["min_debt", "حداقل بدهی (تومان)"], ["max_debt", "حداکثر بدهی (تومان)"],
          ["min_age_days", "حداقل سن بدهی (روز)"], ["max_age_days", "حداکثر سن بدهی (روز)"] ] as const).map(([key, label]) =>
        <NumberField key={key} size="small" label={label} value={filters[key] == null ? "" : String(filters[key])}
          onChange={v => update({ [key]: v === "" ? undefined : Number(v) })} />)}
      <TextField size="small" label="نام یا شناسه نماینده" value={filters.search || ""}
        onChange={e => update({ search: e.target.value })} inputProps={{ maxLength: 100 }} />
    </Stack>
    <Typography variant="caption" color="text.secondary">
      مبلغ، مجموع بدهی پرداخت‌نشدهٔ دوره‌های انتخابی است. سن بدهی از ارسال فاکتور یا مهلت پرداخت جدید محاسبه می‌شود؛
      فاکتور بدون تاریخ ارسال وارد فیلتر سن نمی‌شود. بدون بدهی یعنی هیچ بدهی بازی در هیچ دوره‌ای ندارد.
    </Typography>
    <Stack direction="row" alignItems="center" spacing={1} useFlexGap flexWrap="wrap">
      <FormControlLabel control={<Checkbox checked={filters.exclude_pending || false}
        onChange={e => update({ exclude_pending: e.target.checked })} />} label="حذف کسانی که رسید منتظر تأیید دارند" />
      <Button size="small" onClick={() => onChange({})}>پاک کردن فیلترها</Button>
      {!!filters.excluded_reseller_ids?.length && <Typography variant="caption">
        {filters.excluded_reseller_ids.length} حساب از ارسال کنار گذاشته شده
      </Typography>}
    </Stack>
  </Stack>;
}
