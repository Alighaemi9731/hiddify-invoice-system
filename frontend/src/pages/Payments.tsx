import { useState } from "react";
import {
  Box, Button, Card, Checkbox, Chip, Dialog, DialogActions, DialogContent, DialogTitle,
  Divider, IconButton, MenuItem, Stack, Table, TableBody, TableCell,
  TableHead, TableRow, TextField, Tooltip, Typography, Link,
} from "@mui/material";
import VerifiedIcon from "@mui/icons-material/Verified";
import CheckIcon from "@mui/icons-material/Check";
import CloseIcon from "@mui/icons-material/Close";
import ImageIcon from "@mui/icons-material/Image";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  listPayments, verifyPayment, confirmPayment, rejectPayment, openPaymentProof, getDueInvoices,
} from "../api/client";
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

  // ---- confirm dialog: owner picks which invoices a payment covers ----
  const [confirmRow, setConfirmRow] = useState<any>(null);
  const [selected, setSelected] = useState<Record<number, boolean>>({});
  const { data: dueList = [], isLoading: dueLoading } = useQuery({
    queryKey: ["due-invoices", confirmRow?.id],
    queryFn: () => getDueInvoices(confirmRow.id),
    enabled: !!confirmRow,
  });

  const verify = useMutation({ mutationFn: verifyPayment, onSuccess: (r: any) => { show(r?.message || "بررسی شد"); refresh(); }, onError: (e) => show(errMsg(e), "error") });
  const reject = useMutation({ mutationFn: rejectPayment, onSuccess: (r: any) => { show(r?.message || "رد شد"); refresh(); }, onError: (e) => show(errMsg(e), "error") });
  const confirm_ = useMutation({
    mutationFn: ({ id, ids }: { id: number; ids?: number[] }) => confirmPayment(id, ids),
    onSuccess: (r: any) => { show(r?.message || "تأیید شد"); setConfirmRow(null); refresh(); },
    onError: (e) => show(errMsg(e), "error"),
  });

  const openConfirm = (p: any) => { setSelected({}); setConfirmRow(p); };
  const doReject = (p: any) => {
    const extra = p.status === "confirmed" ? "\n(این پرداخت تأییدشده بود؛ رد آن فاکتورهای تسویه‌شده را دوباره «پرداخت‌نشده» می‌کند.)" : "";
    if (window.confirm(`پرداخت «${p.reseller_name || ""}» رد شود؟${extra}`)) reject.mutate(p.id);
  };

  const selectedIds = dueList.filter((i: any) => selected[i.id]).map((i: any) => i.id);
  const selTotalUsdt = dueList.filter((i: any) => selected[i.id]).reduce((s: number, i: any) => s + (i.amount_usdt || 0), 0);

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
                  <Tooltip title={p.status === "confirmed" ? "تأییدشده" : "تأیید و انتخاب فاکتورها"}><span><IconButton size="small" color="success" disabled={p.status === "confirmed"} onClick={() => openConfirm(p)}><CheckIcon fontSize="small" /></IconButton></span></Tooltip>
                  <Tooltip title={p.status === "rejected" ? "ردشده" : "رد"}><span><IconButton size="small" color="error" disabled={p.status === "rejected"} onClick={() => doReject(p)}><CloseIcon fontSize="small" /></IconButton></span></Tooltip>
                </TableCell>
              </TableRow>
            ))}
            {data.length === 0 && <TableRow><TableCell colSpan={8} align="center" sx={{ py: 4, color: "text.secondary" }}>پرداختی ثبت نشده است</TableCell></TableRow>}
          </TableBody>
        </Table>
      </Card>

      {/* Pick exactly which invoices this payment settles. One transfer can cover several. */}
      <Dialog open={!!confirmRow} onClose={() => setConfirmRow(null)} fullWidth maxWidth="sm">
        {confirmRow && (<>
          <DialogTitle>تأیید پرداخت — {confirmRow.reseller_name}</DialogTitle>
          <DialogContent>
            <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
              فاکتورهایی را که این پرداخت پوشش می‌دهد انتخاب کنید (می‌توانید چند فاکتور را با یک پرداخت تسویه کنید). فقط فاکتورهای انتخاب‌شده «پرداخت‌شده» می‌شوند.
            </Typography>
            {confirmRow.has_proof && (
              <Button size="small" startIcon={<ImageIcon />} onClick={() => openPaymentProof(confirmRow.id)} sx={{ mb: 1 }}>
                مشاهدهٔ رسید
              </Button>
            )}
            {dueLoading && <Typography variant="body2">در حال بارگذاری…</Typography>}
            {!dueLoading && dueList.length === 0 && (
              <Typography variant="body2" color="text.secondary">این مشتری فاکتور پرداخت‌نشدهٔ سررسیده‌ای ندارد.</Typography>
            )}
            {dueList.map((i: any) => (
              <Stack key={i.id} direction="row" alignItems="center" spacing={1} sx={{ py: 0.5 }}>
                <Checkbox size="small" checked={!!selected[i.id]} onChange={(e) => setSelected({ ...selected, [i.id]: e.target.checked })} />
                <Box sx={{ flex: 1 }}>
                  <Typography variant="body2">{i.period_label} — {i.reseller_name} <span style={{ color: "#888" }}>({i.panel_key})</span></Typography>
                </Box>
                <Typography variant="body2" dir="ltr">{fmtUsdt(i.amount_usdt)}</Typography>
              </Stack>
            ))}
            {dueList.length > 0 && <>
              <Divider sx={{ my: 1 }} />
              <Stack direction="row" justifyContent="space-between">
                <Typography variant="body2">جمع انتخاب‌شده: {selectedIds.length} فاکتور</Typography>
                <Typography variant="body2" dir="ltr" fontWeight={600}>{fmtUsdt(selTotalUsdt)}</Typography>
              </Stack>
            </>}
          </DialogContent>
          <DialogActions>
            <Button onClick={() => setConfirmRow(null)}>انصراف</Button>
            <Button onClick={() => confirm_.mutate({ id: confirmRow.id, ids: dueList.map((i: any) => i.id) })}
              disabled={confirm_.isPending || dueList.length === 0}>
              تأیید همهٔ بدهی
            </Button>
            <Button variant="contained" onClick={() => confirm_.mutate({ id: confirmRow.id, ids: selectedIds })}
              disabled={confirm_.isPending || selectedIds.length === 0}>
              تأیید فاکتورهای انتخاب‌شده
            </Button>
          </DialogActions>
        </>)}
      </Dialog>
      {node}
    </Box>
  );
}
