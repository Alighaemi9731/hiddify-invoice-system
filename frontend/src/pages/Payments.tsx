import { useState } from "react";
import {
  Box, Card, Chip, IconButton, MenuItem, Stack, Table, TableBody, TableCell,
  TableHead, TableRow, TextField, Tooltip, Link,
} from "@mui/material";
import VerifiedIcon from "@mui/icons-material/Verified";
import CheckIcon from "@mui/icons-material/Check";
import CloseIcon from "@mui/icons-material/Close";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { listPayments, verifyPayment, confirmPayment, rejectPayment } from "../api/client";
import { useToast, errMsg } from "../components/Toast";
import { useSort, SortTh } from "../components/sortable";
import { fmtUsdt, fmtDate, PAYMENT_STATUS_FA } from "../format";

const COLOR: any = { pending: "warning", confirmed: "success", rejected: "error", duplicate: "default" };

export default function Payments() {
  const qc = useQueryClient();
  const { node, show } = useToast();
  const [status, setStatus] = useState("");
  const { data = [] } = useQuery({
    queryKey: ["payments", status],
    queryFn: () => listPayments({ status: status || undefined }),
  });
  const refresh = () => qc.invalidateQueries({ queryKey: ["payments"] });
  const { sorted, key, dir, toggle } = useSort(data, "created_at", "desc");

  const mk = (fn: (id: number) => Promise<any>, ok: string) =>
    useMutation({ mutationFn: fn, onSuccess: (r: any) => { show(r?.message || ok); refresh(); }, onError: (e) => show(errMsg(e), "error") });
  const verify = mk(verifyPayment, "بررسی شد");
  const confirm_ = mk(confirmPayment, "تأیید شد");
  const reject = mk(rejectPayment, "رد شد");

  return (
    <Box>
      <Stack direction="row" spacing={2} sx={{ mb: 2 }}>
        <TextField select size="small" label="وضعیت" value={status} sx={{ minWidth: 160 }} onChange={(e) => setStatus(e.target.value)}>
          <MenuItem value="">همه</MenuItem>
          {Object.entries(PAYMENT_STATUS_FA).map(([k, v]) => <MenuItem key={k} value={k}>{v}</MenuItem>)}
        </TextField>
      </Stack>
      <Card>
        <Table size="small">
          <TableHead>
            <TableRow>
              <SortTh id="reseller_name" label="نماینده" sortKey={key} dir={dir} onSort={toggle} />
              <SortTh id="method" label="روش" sortKey={key} dir={dir} onSort={toggle} />
              <TableCell>TXID</TableCell>
              <SortTh id="amount_usdt" label="مبلغ" sortKey={key} dir={dir} onSort={toggle} />
              <SortTh id="confirmations" label="تأییدها" sortKey={key} dir={dir} onSort={toggle} />
              <SortTh id="status" label="وضعیت" sortKey={key} dir={dir} onSort={toggle} />
              <SortTh id="created_at" label="تاریخ" sortKey={key} dir={dir} onSort={toggle} />
              <TableCell align="left">عملیات</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {sorted.map((p: any) => (
              <TableRow key={p.id} hover>
                <TableCell>{p.reseller_name}</TableCell>
                <TableCell>{p.method}</TableCell>
                <TableCell dir="ltr" sx={{ maxWidth: 180, overflow: "hidden", textOverflow: "ellipsis" }}>
                  {p.txid ? <Link href={`https://bscscan.com/tx/${p.txid}`} target="_blank">{p.txid.slice(0, 14)}…</Link> : "—"}
                </TableCell>
                <TableCell dir="ltr">{fmtUsdt(p.amount_usdt)}</TableCell>
                <TableCell>{p.confirmations}</TableCell>
                <TableCell><Chip size="small" color={COLOR[p.status]} label={PAYMENT_STATUS_FA[p.status]} /></TableCell>
                <TableCell>{fmtDate(p.created_at)}</TableCell>
                <TableCell align="left">
                  {p.status === "pending" && <>
                    <Tooltip title="بررسی زنجیره"><IconButton size="small" onClick={() => verify.mutate(p.id)}><VerifiedIcon fontSize="small" /></IconButton></Tooltip>
                    <Tooltip title="تأیید دستی"><IconButton size="small" color="success" onClick={() => confirm_.mutate(p.id)}><CheckIcon fontSize="small" /></IconButton></Tooltip>
                    <Tooltip title="رد"><IconButton size="small" color="error" onClick={() => reject.mutate(p.id)}><CloseIcon fontSize="small" /></IconButton></Tooltip>
                  </>}
                </TableCell>
              </TableRow>
            ))}
            {data.length === 0 && <TableRow><TableCell colSpan={8} align="center" sx={{ py: 4, color: "text.secondary" }}>پرداختی ثبت نشده است</TableCell></TableRow>}
          </TableBody>
        </Table>
      </Card>
      {node}
    </Box>
  );
}
