import { useState } from "react";
import {
  Box, Card, Chip, MenuItem, Stack, Table, TableBody, TableCell, TableHead,
  TableRow, TableSortLabel, Typography,
} from "@mui/material";
import { useQuery } from "@tanstack/react-query";
import { getSales } from "../api/client";
import { currentPeriod } from "../components/StatCard";
import PeriodPicker from "../components/PeriodPicker";
import { fmtToman, fmtUsdt, fmtGb, fmtNum, INVOICE_STATUS_FA } from "../format";

const STATUS_COLOR: any = { draft: "default", sent: "info", paid: "success", overdue: "warning", enforced: "error" };

export default function Sales() {
  const [period, setPeriod] = useState(currentPeriod());
  const [sort, setSort] = useState("amount");
  const [order, setOrder] = useState<"asc" | "desc">("desc");
  const { data = [] } = useQuery({
    queryKey: ["sales", period, sort, order],
    queryFn: () => getSales({ period, sort, order }),
  });
  const total = data.reduce((s: number, r: any) => s + r.amount_toman, 0);
  const totalGb = data.reduce((s: number, r: any) => s + r.usage_gb, 0);

  const head = (id: string, label: string) => (
    <TableCell sortDirection={sort === id ? order : false}>
      <TableSortLabel active={sort === id} direction={sort === id ? order : "desc"}
        onClick={() => { setSort(id); setOrder(sort === id && order === "desc" ? "asc" : "desc"); }}>
        {label}
      </TableSortLabel>
    </TableCell>
  );

  return (
    <Box>
      <Stack direction="row" spacing={2} sx={{ mb: 2 }} alignItems="center">
        <PeriodPicker value={period} onChange={setPeriod} />
        <Box sx={{ flexGrow: 1 }} />
        <Typography variant="body2" color="text.secondary">
          {fmtNum(data.length)} فاکتور — {fmtGb(totalGb)} — جمع {fmtToman(total)}
        </Typography>
      </Stack>
      <Card>
        <Table size="small">
          <TableHead>
            <TableRow>
              {head("name", "نماینده")}
              <TableCell>پنل</TableCell>
              {head("usage", "مصرف")}
              {head("amount", "مبلغ (تومان)")}
              <TableCell>USDT</TableCell><TableCell>وضعیت</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {data.map((r: any) => (
              <TableRow key={r.invoice_id} hover>
                <TableCell>{r.reseller_name}</TableCell>
                <TableCell>{r.panel_key}</TableCell>
                <TableCell>{fmtGb(r.usage_gb)}</TableCell>
                <TableCell>{fmtToman(r.amount_toman)}</TableCell>
                <TableCell dir="ltr">{fmtUsdt(r.amount_usdt)}</TableCell>
                <TableCell><Chip size="small" color={STATUS_COLOR[r.status]} label={INVOICE_STATUS_FA[r.status]} /></TableCell>
              </TableRow>
            ))}
            {data.length === 0 && <TableRow><TableCell colSpan={6} align="center" sx={{ py: 4, color: "text.secondary" }}>داده‌ای برای این دوره نیست</TableCell></TableRow>}
          </TableBody>
        </Table>
      </Card>
    </Box>
  );
}
