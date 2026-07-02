/**
 * Shared client-side CSV export (extracted from the FinancialHistory page so every
 * page produces the same file shape). The leading UTF-8 BOM makes Excel open the
 * Persian text with the right encoding; every cell is quoted with `"` doubled.
 * Headers are written as-is (unquoted), matching the original export byte-for-byte.
 */
export function downloadCsv(
  filename: string,
  headers: string[],
  rows: (string | number | null | undefined)[][],
): void {
  const lines = rows.map((r) =>
    r.map((v) => `"${String(v ?? "").replace(/"/g, '""')}"`).join(","),
  );
  const csv = "﻿" + [headers.join(","), ...lines].join("\n");
  const url = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 60000);
}
