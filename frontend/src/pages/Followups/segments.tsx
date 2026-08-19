import { Chip } from "@mui/material";
import type { CrmSegment } from "../../api/client";

type ChipColor = "default" | "primary" | "error" | "info" | "success" | "warning";

/**
 * The segment vocabulary, in the SAME priority order the backend classifies with
 * (`app/services/crm.py::SEGMENTS`) — first match wins, so a reseller is in exactly one.
 *
 * Colour comes from the MUI `color` prop, never a hex (DESIGN_SYSTEM §2.5: semantic status is
 * expressed through the palette, not literal values). There are only six visually distinct
 * `color` values but eleven segments, so each colour is paired with a `variant`: filled reads
 * as the more urgent half of its pair. `primary`/filled is the ONE combination left unused —
 * the filter chips paint the SELECTED chip that way, so a segment wearing it would read as
 * permanently selected. `unregistered` therefore takes `primary`/outlined only.
 */
export const SEGMENTS: {
  key: CrmSegment; label: string; color: ChipColor; variant: "filled" | "outlined"; help: string;
}[] = [
  { key: "suspended", label: "مسدود", color: "error", variant: "filled",
    help: "پنلشان به‌خاطر بدهی بسته شده — تا تسویه نکنند کاربری نمی‌سازند." },
  { key: "frozen", label: "منجمد", color: "error", variant: "outlined",
    help: "سقف کاربرشان صفر شده؛ کاربران فعلی آنلاین‌اند ولی کاربر جدید نمی‌توانند بسازند." },
  { key: "debtor", label: "بدهکار", color: "warning", variant: "filled",
    help: "فاکتور پرداخت‌نشدهٔ سررسیدشده دارند (مهلت پرداخت آینده حساب نمی‌شود)." },
  { key: "unregistered", label: "وصل‌نشده به ربات", color: "primary", variant: "outlined",
    help: "هنوز لینک پنلشان را به ربات نداده‌اند؛ فاکتور، یادآوری و پیام به دستشان نمی‌رسد." },
  { key: "churned", label: "ریزش‌کرده", color: "warning", variant: "outlined",
    help: "خیلی وقت است هیچ سرویس جدیدی نساخته‌اند — عملاً از دستمان رفته‌اند." },
  { key: "never_active", label: "هرگز فعال نشده", color: "default", variant: "filled",
    help: "پنل گرفته‌اند ولی حتی یک سرویس قابل‌فاکتور هم نساخته‌اند." },
  { key: "dormant", label: "خوابیده", color: "default", variant: "outlined",
    help: "مدتی است سرویس جدیدی نساخته‌اند؛ هنوز قابل برگرداندن‌اند." },
  { key: "onboarding", label: "تازه‌وارد", color: "info", variant: "filled",
    help: "حساب‌شان آن‌قدر جدید است که هنوز نمی‌شود قضاوت کرد." },
  { key: "declining", label: "رو به افول", color: "info", variant: "outlined",
    help: "هنوز می‌فروشند، ولی روند این ماهشان به‌وضوح از میانگین سه ماه گذشته کمتر است." },
  { key: "growing", label: "در حال رشد", color: "success", variant: "outlined",
    help: "روند این ماهشان از میانگین سه ماه گذشته بیشتر است." },
  { key: "healthy", label: "سالم", color: "success", variant: "filled",
    help: "به‌طور منظم می‌فروشند و بدهی سررسیدشده ندارند." },
];

const BY_KEY = new Map(SEGMENTS.map((s) => [s.key, s]));

export const segmentLabel = (key: string) => BY_KEY.get(key as CrmSegment)?.label || key;

export function SegmentChip({ segment, onClick }: { segment: string; onClick?: () => void }) {
  const def = BY_KEY.get(segment as CrmSegment);
  return (
    <Chip
      size="small"
      color={def?.color || "default"}
      variant={def?.variant || "outlined"}
      label={def?.label || segment}
      onClick={onClick}
      sx={onClick ? { cursor: "pointer" } : undefined}
    />
  );
}
