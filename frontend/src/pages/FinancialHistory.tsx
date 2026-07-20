import { useEffect, useState } from "react";
import {
  Box, Button, Card, Chip, InputAdornment, MenuItem, Select, Stack, Table, TableBody, TableCell,
  TableContainer, TableHead, TableRow, TextField, Typography,
} from "@mui/material";
import DownloadIcon from "@mui/icons-material/esm/Download";
import SearchIcon from "@mui/icons-material/esm/Search";
import { useQuery } from "@tanstack/react-query";
import { getFinancialHistory } from "../api/client";
import { downloadCsv } from "../csv";
import PeriodPicker from "../components/PeriodPicker";
import { DataState } from "../components/DataState";
import { TablePager } from "../components/TablePager";
import { fmtToman, fmtGb, fmtNum, fmtDate, INVOICE_STATUS_FA } from "../format";

const STATUS_COLOR: any = {
  draft: "default", sent: "info", paid: "success", overdue: "warning",
  enforced: "error", canceled: "default",
};

export default function FinancialHistory() {
  const [period, setPeriod] = useState("");   // empty = all months
  const [q, setQ] = useState("");
  const [status, setStatus] = useState("");
  const [page, setPage] = useState(0);
  const [rpp, setRpp] = useState(50);
  useEffect(() => setPage(0), [period, q, status]);

  const { data = [], isLoading, isError, refetch } = useQuery({
    queryKey: ["financial-history", period, q, status],
    queryFn: () => getFinancialHistory({
      period: period || undefined, q: q || undefined, status: status || undefined,
    }),
  });
  const paged = data.slice(page * rpp, page * rpp + rpp);

  const total = data.reduce((s: number, r: any) => s + r.amount_toman, 0);
  const paid = data.filter((r: any) => r.status === "paid")
    .reduce((s: number, r: any) => s + r.amount_toman, 0);

  const exportCsv = () => downloadCsv(
    "financial-history.csv",
    ["پنل", "نماینده", "UUID", "دوره", "مصرف (گیگابایت)", "قیمت هر گیگابایت", "مبلغ (تومان)", "وضعیت", "تاریخ پرداخت", "TXID"],
    data.map((r: any) => [
      r.panel_key, r.reseller_name, r.reseller_admin_uuid, r.period_label, r.usage_gb,
      r.price_per_gb, r.amount_toman, r.status, r.paid_at || "", r.txid || "",
    ]),
  );

  return (
    <Box>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
        تاریخچهٔ دائمی مالی: مبلغ و وضعیت پرداختِ فاکتور هر نماینده در هر ماه.
        این سوابق حتی پس از «پاک‌سازی داده‌ها» یا حذف پنل یا نماینده حفظ می‌شوند.
      </Typography>
      <Stack direction={{ xs: "column", sm: "row" }} spacing={1.5} sx={{ mb: 2 }} alignItems="center">
        <PeriodPicker value={period} onChange={setPeriod} allowEmpty />
        <TextField size="small" value={q} placeholder="جستجوی نماینده…" onChange={(e) => setQ(e.target.value)}
          InputProps={{ startAdornment: <InputAdornment position="start"><SearchIcon fontSize="small" /></InputAdornment> }} />
        <Select
          size="small"
          value={status}
          displayEmpty
          onChange={(e) => setStatus(e.target.value)}
          renderValue={(v) => v ? INVOICE_STATUS_FA[v] : "همهٔ وضعیت‌ها"}
          sx={{ minWidth: 148, "& .MuiSelect-select": { py: "7px !important" } }}
        >
          <MenuItem value="">همهٔ وضعیت‌ها</MenuItem>
          {Object.entries(INVOICE_STATUS_FA).map(([k, v]) => <MenuItem key={k} value={k}>{v}</MenuItem>)}
        </Select>
        <Box sx={{ flexGrow: 1 }} />
        <Typography variant="body2" color="text.secondary">
          {fmtNum(data.length)} سطر — جمع {fmtToman(total)} — پرداخت‌شده {fmtToman(paid)}
        </Typography>
        <Button size="small" variant="outlined" startIcon={<DownloadIcon />} onClick={exportCsv}
          disabled={data.length === 0}>CSV</Button>
      </Stack>
      <DataState isLoading={isLoading} isError={isError} onRetry={refetch}>
      <Card>
        <TableContainer sx={{ maxHeight: { xs: "none", sm: "calc(100vh - 260px)" } }}>
          <Table size="small" stickyHeader className="resp-table" sx={{ minWidth: { sm: 720 } }}>
            <TableHead>
              <TableRow>
                <TableCell>پنل</TableCell>
                <TableCell>نماینده</TableCell>
                <TableCell>دوره</TableCell>
                <TableCell>مصرف</TableCell>
                <TableCell>مبلغ (تومان)</TableCell>
                <TableCell>وضعیت</TableCell>
                <TableCell>تاریخ پرداخت</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {paged.map((r: any) => (
                <TableRow key={r.id} hover>
                  <TableCell>{r.panel_key}</TableCell>
                  <TableCell>{r.reseller_name}</TableCell>
                  <TableCell dir="ltr">{r.period_label}</TableCell>
                  <TableCell>{fmtGb(r.usage_gb)}</TableCell>
                  <TableCell>{fmtToman(r.amount_toman)}</TableCell>
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
        </TableContainer>
        <TablePager count={data.length} page={page} rpp={rpp} onPage={setPage}
          onRpp={(v) => { setRpp(v); setPage(0); }} />
      </Card>
      </DataState>
    </Box>
  );
}
