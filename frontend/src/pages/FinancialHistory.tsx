import { useState } from "react";
import {
  Box, Button, Card, Chip, Stack, Table, TableBody, TableCell, TableHead,
  TableRow, TextField, Typography, MenuItem,
} from "@mui/material";
import DownloadIcon from "@mui/icons-material/Download";
import { useQuery } from "@tanstack/react-query";
import { getFinancialHistory } from "../api/client";
import PeriodPicker from "../components/PeriodPicker";
import { fmtToman, fmtUsdt, fmtGb, fmtNum, fmtDate, INVOICE_STATUS_FA } from "../format";

const STATUS_COLOR: any = {
  draft: "default", sent: "info", paid: "success", overdue: "warning",
  enforced: "error", canceled: "default",
};

export default function FinancialHistory() {
  const [period, setPeriod] = useState("");   // empty = all months
  const [q, setQ] = useState("");
  const [status, setStatus] = useState("");

  const { data = [] } = useQuery({
    queryKey: ["financial-history", period, q, status],
    queryFn: () => getFinancialHistory({
      period: period || undefined, q: q || undefined, status: status || undefined,
    }),
  });

  const total = data.reduce((s: number, r: any) => s + r.amount_toman, 0);
  const paid = data.filter((r: any) => r.status === "paid")
    .reduce((s: number, r: any) => s + r.amount_toman, 0);

  const exportCsv = () => {
    const head = ["پنل", "نماینده", "UUID", "دوره", "گیگ", "قیمت/گیگ", "تومان", "USDT", "وضعیت", "تاریخ پرداخت", "TXID"];
    const lines = data.map((r: any) => [
      r.panel_key, r.reseller_name, r.reseller_admin_uuid, r.period_label, r.usage_gb,
      r.price_per_gb, r.amount_toman, r.amount_usdt, r.status, r.paid_at || "", r.txid || "",
    ].map((v) => `"${String(v ?? "").replace(/"/g, '""')}"`).join(","));
    const csv = "﻿" + [head.join(","), ...lines].join("\n");
    const url = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }));
    const a = document.createElement("a");
    a.href = url; a.download = "financial-history.csv"; a.click();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  };

  return (
    <Box>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
        تاریخچهٔ دائمی مالی: هر فاکتور هر نماینده در هر ماه — مبلغ و وضعیت پرداخت.
        این سوابق حتی پس از «پاک‌سازی داده‌ها» یا حذف پنل/نماینده باقی می‌مانند.
      </Typography>
      <Stack direction={{ xs: "column", sm: "row" }} spacing={1.5} sx={{ mb: 2 }} alignItems="center">
        <PeriodPicker value={period} onChange={setPeriod} label="دوره (خالی = همه)" allowEmpty />
        <TextField size="small" label="جستجوی نماینده" value={q} onChange={(e) => setQ(e.target.value)} />
        <TextField select size="small" label="وضعیت" value={status} sx={{ minWidth: 140 }}
          onChange={(e) => setStatus(e.target.value)}>
          <MenuItem value="">همه</MenuItem>
          {Object.entries(INVOICE_STATUS_FA).map(([k, v]) => <MenuItem key={k} value={k}>{v}</MenuItem>)}
        </TextField>
        <Box sx={{ flexGrow: 1 }} />
        <Typography variant="body2" color="text.secondary">
          {fmtNum(data.length)} سطر — جمع {fmtToman(total)} — پرداخت‌شده {fmtToman(paid)}
        </Typography>
        <Button size="small" variant="outlined" startIcon={<DownloadIcon />} onClick={exportCsv}
          disabled={data.length === 0}>CSV</Button>
      </Stack>
      <Card>
        <Table size="small">
          <TableHead>
            <TableRow>
              <TableCell>پنل</TableCell>
              <TableCell>نماینده</TableCell>
              <TableCell>دوره</TableCell>
              <TableCell>مصرف</TableCell>
              <TableCell>مبلغ (تومان)</TableCell>
              <TableCell>USDT</TableCell>
              <TableCell>وضعیت</TableCell>
              <TableCell>تاریخ پرداخت</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {data.map((r: any) => (
              <TableRow key={r.id} hover>
                <TableCell>{r.panel_key}</TableCell>
                <TableCell>{r.reseller_name}</TableCell>
                <TableCell dir="ltr">{r.period_label}</TableCell>
                <TableCell>{fmtGb(r.usage_gb)}</TableCell>
                <TableCell>{fmtToman(r.amount_toman)}</TableCell>
                <TableCell dir="ltr">{fmtUsdt(r.amount_usdt)}</TableCell>
                <TableCell>
                  <Chip size="small" color={STATUS_COLOR[r.status] || "default"}
                    label={INVOICE_STATUS_FA[r.status] || r.status} />
                </TableCell>
                <TableCell>{r.paid_at ? fmtDate(r.paid_at) : "—"}</TableCell>
              </TableRow>
            ))}
            {data.length === 0 && (
              <TableRow><TableCell colSpan={8} align="center" sx={{ py: 4, color: "text.secondary" }}>
                هنوز سابقه‌ای ثبت نشده است
              </TableCell></TableRow>
            )}
          </TableBody>
        </Table>
      </Card>
    </Box>
  );
}
