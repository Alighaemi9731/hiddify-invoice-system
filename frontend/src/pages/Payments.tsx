import { useState } from "react";
import {
  Box, Button, Card, Chip, Dialog, DialogActions, DialogContent, DialogTitle,
  IconButton, MenuItem, Stack, Table, TableBody, TableCell,
  TableHead, TableRow, TextField, Tooltip, Typography, Link,
} from "@mui/material";
import VerifiedIcon from "@mui/icons-material/Verified";
import CheckIcon from "@mui/icons-material/Check";
import CloseIcon from "@mui/icons-material/Close";
import ImageIcon from "@mui/icons-material/Image";
import DeleteOutlineIcon from "@mui/icons-material/DeleteOutline";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  listPayments, verifyPayment, confirmPayment, rejectPayment, deletePayment, openPaymentProof,
} from "../api/client";
import { useToast, errMsg } from "../components/Toast";
import { useSort, SortTh } from "../components/sortable";
import { DataState } from "../components/DataState";
import { fmtToman, fmtDate, PAYMENT_STATUS_FA, PAYMENT_METHOD_FA } from "../format";

const COLOR: any = { pending: "warning", confirmed: "success", rejected: "error", duplicate: "default" };

export default function Payments() {
  const qc = useQueryClient();
  const { node, show } = useToast();
  const [status, setStatus] = useState("");
  const [search, setSearch] = useState("");
  const { data = [], isLoading, isError, refetch } = useQuery({
    queryKey: ["payments", status],
    queryFn: () => listPayments({ status: status || undefined }),
  });
  // A confirm/reject/delete can flip an invoice paid↔owed, so refresh the dependent views too.
  const refresh = () => {
    ["payments", "invoices", "dashboard", "debts"].forEach((k) => qc.invalidateQueries({ queryKey: [k] }));
  };
  const { sorted, key, dir, toggle } = useSort(data, "created_at", "desc");
  // Search by tracking number (the «#N» the customer quotes) or reseller name. Persian/Arabic
  // digits are normalized to ASCII so a hand-typed «#۱۲» matches the (ASCII) id «12».
  const toAscii = (s: string) =>
    s.replace(/[۰-۹]/g, (d) => "۰۱۲۳۴۵۶۷۸۹".indexOf(d).toString())
     .replace(/[٠-٩]/g, (d) => "٠١٢٣٤٥٦٧٨٩".indexOf(d).toString());
  const q = toAscii(search.trim().replace(/^#/, "")).toLowerCase();
  const shown = q
    ? sorted.filter((p: any) => String(p.id).includes(q) || (p.reseller_name || "").toLowerCase().includes(q))
    : sorted;

  // ---- confirm dialog: a payment is for ONE invoice; the owner just confirms it ----
  const [confirmRow, setConfirmRow] = useState<any>(null);

  const verify = useMutation({ mutationFn: verifyPayment, onSuccess: (r: any) => { show(r?.message || "بررسی شد"); refresh(); }, onError: (e) => show(errMsg(e), "error") });
  const reject = useMutation({ mutationFn: rejectPayment, onSuccess: (r: any) => { show(r?.message || "رد شد"); refresh(); }, onError: (e) => show(errMsg(e), "error") });
  const confirm_ = useMutation({
    mutationFn: (id: number) => confirmPayment(id),
    onSuccess: (r: any) => { show(r?.message || "تأیید شد"); setConfirmRow(null); refresh(); },
    onError: (e) => show(errMsg(e), "error"),
  });

  const del = useMutation({ mutationFn: deletePayment, onSuccess: (r: any) => { show(r?.message || "حذف شد"); refresh(); }, onError: (e) => show(errMsg(e), "error") });

  const doReject = (p: any) => {
    const extra = p.status === "confirmed" ? "\n(این پرداخت تأییدشده بود؛ رد آن فاکتورِ تسویه‌شده را دوباره «پرداخت‌نشده» می‌کند.)" : "";
    if (window.confirm(`پرداخت «${p.reseller_name || ""}» رد شود؟${extra}`)) reject.mutate(p.id);
  };
  const doDelete = (p: any) => {
    const extra = p.status === "confirmed" ? "\n(این پرداخت تأییدشده بود؛ با حذف، فاکتورِ مرتبط دوباره «پرداخت‌نشده» می‌شود.)" : "";
    if (window.confirm(`پرداختِ «${p.reseller_name || ""}» (دوره ${p.invoice_period || "—"}) برای همیشه حذف شود؟${extra}`)) del.mutate(p.id);
  };

  return (
    <Box>
      <Stack direction={{ xs: "column", sm: "row" }} spacing={1.5} sx={{ mb: 2 }}>
        <TextField select size="small" label="وضعیت" value={status} sx={{ minWidth: 160 }} onChange={(e) => setStatus(e.target.value)}>
          <MenuItem value="">همه</MenuItem>
          {Object.entries(PAYMENT_STATUS_FA).map(([k, v]) => <MenuItem key={k} value={k}>{v}</MenuItem>)}
        </TextField>
        <TextField size="small" label="جستجوی شمارهٔ پیگیری یا نام" value={search} sx={{ minWidth: { sm: 240 } }}
          placeholder="مثلاً #۱۲ یا نام نماینده" onChange={(e) => setSearch(e.target.value)} />
      </Stack>
      <DataState isLoading={isLoading} isError={isError} onRetry={refetch}>
      <Card>
        <Table size="small" className="resp-table">
          <TableHead>
            <TableRow>
              <SortTh id="id" label="#" sortKey={key} dir={dir} onSort={toggle} />
              <SortTh id="reseller_name" label="نماینده" sortKey={key} dir={dir} onSort={toggle} />
              <SortTh id="invoice_period" label="فاکتور (دوره)" sortKey={key} dir={dir} onSort={toggle} />
              <SortTh id="method" label="روش" sortKey={key} dir={dir} onSort={toggle} />
              <TableCell>TXID</TableCell>
              <SortTh id="invoice_amount_toman" label="مبلغ" sortKey={key} dir={dir} onSort={toggle} />
              <SortTh id="confirmations" label="تأییدها" sortKey={key} dir={dir} onSort={toggle} />
              <SortTh id="status" label="وضعیت" sortKey={key} dir={dir} onSort={toggle} />
              <SortTh id="created_at" label="تاریخ" sortKey={key} dir={dir} onSort={toggle} />
              <TableCell align="left">عملیات</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {shown.map((p: any) => (
              <TableRow key={p.id} hover>
                <TableCell data-label="#" dir="ltr" sx={{ color: "text.secondary", fontWeight: 600 }}>#{p.id}</TableCell>
                <TableCell data-label="نماینده">
                  {/* Click the name → open the customer's Telegram PV (username if known, else by id). */}
                  {p.reseller_username
                    ? <Tooltip title="باز کردن گفتگوی تلگرام"><Link href={`https://t.me/${p.reseller_username}`} target="_blank" rel="noopener" underline="hover">{p.reseller_name}</Link></Tooltip>
                    : p.reseller_chat_id
                      ? <Tooltip title="باز کردن گفتگوی تلگرام (با شناسهٔ عددی)"><Link href={`tg://user?id=${p.reseller_chat_id}`} underline="hover">{p.reseller_name}</Link></Tooltip>
                      : p.reseller_name}
                </TableCell>
                <TableCell data-label="فاکتور (دوره)">{p.invoice_period || "—"}</TableCell>
                <TableCell data-label="روش">{PAYMENT_METHOD_FA[p.method] || p.method}</TableCell>
                <TableCell data-label="TXID" dir="ltr" sx={{ maxWidth: 180, overflow: "hidden", textOverflow: "ellipsis" }}>
                  {/* Click the hash → open it on the matching explorer (TON → tonscan, else bscscan)
                      so the owner can verify it manually before confirming. */}
                  {p.txid
                    ? <Tooltip title="باز کردن در اکسپلورر برای بررسی"><Link href={p.chain === "ton" ? `https://tonscan.org/tx/${p.txid}` : `https://bscscan.com/tx/${p.txid}`} target="_blank" rel="noopener">{p.txid.slice(0, 14)}…</Link></Tooltip>
                    : p.has_proof
                      ? <Tooltip title="مشاهدهٔ رسید"><IconButton size="small" onClick={() => openPaymentProof(p.id)}><ImageIcon fontSize="small" /></IconButton></Tooltip>
                      : "—"}
                </TableCell>
                <TableCell data-label="مبلغ" dir="ltr">
                  <Tooltip title={
                    <span style={{ whiteSpace: "pre-line" }}>
                      {`فاکتور: ${p.invoice_amount_toman ? fmtToman(p.invoice_amount_toman) : "—"}${p.invoice_equiv ? "\nمعادل: " + p.invoice_equiv : ""}`}
                    </span>
                  }>
                    <span style={{ cursor: "help" }}>
                      {p.invoice_amount_toman ? fmtToman(p.invoice_amount_toman) : "—"}
                    </span>
                  </Tooltip>
                </TableCell>
                <TableCell data-label="تأییدها">{p.confirmations}</TableCell>
                <TableCell data-label="وضعیت"><Chip size="small" color={COLOR[p.status]} label={PAYMENT_STATUS_FA[p.status]} /></TableCell>
                <TableCell data-label="تاریخ">{fmtDate(p.created_at)}</TableCell>
                <TableCell data-label="عملیات" align="left">
                  {/* Actions stay available for every status so a wrong choice is reversible. */}
                  {/* Optional on-chain check — USDT/BSC only (no TON verifier). Legacy rows have
                      chain='' (treated as bsc); only an explicit TON row disables it. */}
                  <Tooltip title={p.chain === "ton" ? "بررسی زنجیره فقط برای USDT است؛ TON را با لینک بررسی کنید" : "بررسی زنجیره (USDT)"}><span><IconButton size="small" disabled={!p.txid || p.chain === "ton"} onClick={() => verify.mutate(p.id)}><VerifiedIcon fontSize="small" /></IconButton></span></Tooltip>
                  <Tooltip title={p.status === "confirmed" ? "تأییدشده" : "تأیید پرداخت"}><span><IconButton size="small" color="success" disabled={p.status === "confirmed"} onClick={() => setConfirmRow(p)}><CheckIcon fontSize="small" /></IconButton></span></Tooltip>
                  <Tooltip title={p.status === "rejected" ? "ردشده" : "رد"}><span><IconButton size="small" color="error" disabled={p.status === "rejected"} onClick={() => doReject(p)}><CloseIcon fontSize="small" /></IconButton></span></Tooltip>
                  <Tooltip title="حذف کامل (برای پاک‌سازی داده‌های تستی)"><span><IconButton size="small" disabled={del.isPending} onClick={() => doDelete(p)}><DeleteOutlineIcon fontSize="small" /></IconButton></span></Tooltip>
                </TableCell>
              </TableRow>
            ))}
            {shown.length === 0 && <TableRow><TableCell colSpan={10} align="center" sx={{ py: 4, color: "text.secondary" }}>{q ? "پرداختی با این جستجو یافت نشد" : "پرداختی ثبت نشده است"}</TableCell></TableRow>}
          </TableBody>
        </Table>
      </Card>
      </DataState>

      {/* A payment is for ONE invoice — just confirm it (view the receipt first if a screenshot). */}
      <Dialog open={!!confirmRow} onClose={() => setConfirmRow(null)} fullWidth maxWidth="xs">
        {confirmRow && (<>
          <DialogTitle>تأیید پرداخت</DialogTitle>
          <DialogContent>
            <Typography variant="body2" sx={{ mb: 1 }}>
              نماینده: <b>{confirmRow.reseller_name}</b>
            </Typography>
            <Typography variant="body2" sx={{ mb: 1 }}>
              فاکتور دوره: <b>{confirmRow.invoice_period || "—"}</b>
              {confirmRow.invoice_amount_toman ? <> — {fmtToman(confirmRow.invoice_amount_toman)}</> : null}
            </Typography>
            <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
              با تأیید، فقط همین فاکتور «پرداخت‌شده» می‌شود.
            </Typography>
            {confirmRow.has_proof && (
              <Button size="small" startIcon={<ImageIcon />} onClick={() => openPaymentProof(confirmRow.id)}>
                مشاهدهٔ رسید
              </Button>
            )}
          </DialogContent>
          <DialogActions>
            <Button onClick={() => setConfirmRow(null)}>انصراف</Button>
            <Button variant="contained" disabled={confirm_.isPending}
              onClick={() => confirm_.mutate(confirmRow.id)}>
              تأیید پرداخت
            </Button>
          </DialogActions>
        </>)}
      </Dialog>
      {node}
    </Box>
  );
}
