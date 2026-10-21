/** Neutralize spreadsheet formulas in reseller-controlled names before CSV export. */
export function recipientCsv(rows: any[]): string {
  const cell = (v: unknown) => {
    let text = String(v ?? "");
    if (/^[\s]*[=+\-@\t\r]/.test(text)) text = "'" + text;
    return '"' + text.replace(/"/g, '""') + '"';
  };
  const data = [["شناسه", "نماینده", "پنل", "ماه فاکتور", "بدهی تومان", "سن بدهی", "دسترسی", "وضعیت ربات"],
    ...rows.map(r => [r.reseller_id, r.name, r.panel, (r.periods || []).join(" / "),
      r.debt_toman, r.oldest_debt_days, r.access_state, r.status])];
  return "\uFEFF" + data.map(row => row.map(cell).join(",")).join("\r\n");
}
