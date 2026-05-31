import { useState } from "react";
import {
  Box, Card, Chip, IconButton, MenuItem, Stack, Table, TableBody, TableCell,
  TableHead, TableRow, TextField, Tooltip, Link,
} from "@mui/material";
import VerifiedIcon from "@mui/icons-material/Verified";
import CheckIcon from "@mui/icons-material/Check";
import CloseIcon from "@mui/icons-material/Close";
import ImageIcon from "@mui/icons-material/Image";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { listPayments, verifyPayment, confirmPayment, rejectPayment, openPaymentProof } from "../api/client";
import { useToast, errMsg } from "../components/Toast";
import { useSort, SortTh } from "../components/sortable";
import { fmtUsdt, fmtDate, PAYMENT_STATUS_FA, PAYMENT_METHOD_FA } from "../format";

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

  // Guard the money-changing actions behind a confirm dialog so a mis-tap doesn't
  // confirm/reject by accident. The buttons stay available for every status, so a wrong
  // choice can always be reversed by clicking the other one.
  const doConfirm = (p: any) => {
    if (p.status === "confirmed") { show("این پرداخت قبلاً تأیید شده است."); return; }
    const extra = p.status === "rejected" ? "\n(این پرداخت قبلاً رد شده بود و دوباره تأیید می‌شود.)" : "";
    if (window.confirm(`پرداخت «${p.reseller_name || ""}» تأیید شود؟ فاکتور پرداخت‌شده و در صورت مسدودی، نماینده آزاد می‌شود.${extra}`))
      confirm_.mutate(p.id);
  };
  const doReject = (p: any) => {
    const extra = p.status === "confirmed" ? "\n(این پرداخت تأییدشده بود؛ رد آن فاکتور را دوباره «پرداخت‌نشده» می‌کند.)" : "";
    if (window.confirm(`پرداخت «${p.reseller_name || ""}» رد شود؟${extra}`))
      reject.mutate(p.id);
  };

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
                <TableCell>{PAYMENT_METHOD_FA[p.method] || p.method}</TableCell>
                <TableCell dir="ltr" sx={{ maxWidth: 180, overflow: "hidden", textOverflow: "ellipsis" }}>
                  {p.txid
                    ? <Link href={`https://bscscan.com/tx/${p.txid}`} target="_blank">{p.txid.slice(0, 14)}…</Link>
                    : p.has_proof
                      ? <Tooltip title="مشاهدهٔ رسید"><IconButton size="small" onClick={() => openPaymentProof(p.id)}><ImageIcon fontSize="small" /></IconButton></Tooltip>
                      : "—"}
                </TableCell>
                <TableCell dir="ltr">{fmtUsdt(p.amount_usdt)}</TableCell>
                <TableCell>{p.confirmations}</TableCell>
                <TableCell><Chip size="small" color={COLOR[p.status]} label={PAYMENT_STATUS_FA[p.status]} /></TableCell>
                <TableCell>{fmtDate(p.created_at)}</TableCell>
                <TableCell align="left">
                  {/* Actions stay available for every status so a wrong choice is reversible. */}
                  <Tooltip title="بررسی زنجیره (TXID)"><span><IconButton size="small" disabled={!p.txid} onClick={() => verify.mutate(p.id)}><VerifiedIcon fontSize="small" /></IconButton></span></Tooltip>
                  <Tooltip title={p.status === "confirmed" ? "تأییدشده" : "تأیید دستی"}><span><IconButton size="small" color="success" disabled={p.status === "confirmed"} onClick={() => doConfirm(p)}><CheckIcon fontSize="small" /></IconButton></span></Tooltip>
                  <Tooltip title={p.status === "rejected" ? "ردشده" : "رد"}><span><IconButton size="small" color="error" disabled={p.status === "rejected"} onClick={() => doReject(p)}><CloseIcon fontSize="small" /></IconButton></span></Tooltip>
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
