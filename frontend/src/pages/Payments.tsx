import { useState } from "react";
import {
  Box, Button, Card, Chip, Dialog, DialogActions, DialogContent, DialogTitle,
  IconButton, InputAdornment, MenuItem, Select, Stack, Table, TableBody, TableCell,
  TableHead, TableRow, TextField, Tooltip, Typography, Link,
} from "@mui/material";
import SearchIcon from "@mui/icons-material/esm/Search";
import VerifiedIcon from "@mui/icons-material/esm/Verified";
import CheckIcon from "@mui/icons-material/esm/Check";
import CloseIcon from "@mui/icons-material/esm/Close";
import ImageIcon from "@mui/icons-material/esm/Image";
import DeleteOutlineIcon from "@mui/icons-material/esm/DeleteOutline";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  listPayments, confirmPayment, rejectPayment, deletePayment, openPaymentProof,
  depositCheck,
} from "../api/client";
import { useToast, errMsg } from "../components/Toast";
import { useSort, SortTh } from "../components/sortable";
import { DataState } from "../components/DataState";
import { fmtToman, fmtDate, PAYMENT_STATUS_FA, PAYMENT_METHOD_FA } from "../format";

const COLOR: any = { pending: "warning", confirmed: "success", rejected: "error" };

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
  // Search by tracking number (the public «#N» the customer quotes) or reseller name.
  // Persian/Arabic digits are normalized to ASCII so a hand-typed «#۱۲» matches the code.
  const toAscii = (s: string) =>
    s.replace(/[۰-۹]/g, (d) => "۰۱۲۳۴۵۶۷۸۹".indexOf(d).toString())
     .replace(/[٠-٩]/g, (d) => "٠١٢٣٤٥٦٧٨٩".indexOf(d).toString());
  const q = toAscii(search.trim().replace(/^#/, "")).toLowerCase();
  const shown = q
    ? sorted.filter((p: any) => String(p.number || "").includes(q) || (p.reseller_name || "").toLowerCase().includes(q))
    : sorted;

  // ---- confirm dialog: a payment is for ONE invoice; the owner just confirms it ----
  const [confirmRow, setConfirmRow] = useState<any>(null);

  // For a crypto payment under review, read the actual on-chain deposit (best-effort) so the
  // operator can match it against the invoice before confirming. Fetched only when the dialog opens.
  const depChk = useQuery({
    queryKey: ["deposit-check", confirmRow?.id],
    queryFn: () => depositCheck(confirmRow.id),
    enabled: !!confirmRow && !!confirmRow.txid,
    staleTime: 30_000,
  });

  // No auto-verify anywhere; this just reads the actual on-chain deposit (TON via toncenter, USDT
  // via a public BSC RPC node — both free) and reports it for the manual confirm decision.
  const depLabel = (r: any) => r.kind === "ton"
    ? `${r.received_ton} GRAM ≈ ${fmtToman(r.received_toman)} | فاکتور: ${fmtToman(r.invoice_toman)}`
    : r.kind === "avax"
    ? `${r.received_avax} AVAX ≈ ${fmtToman(r.received_toman)}${r.confirmations != null ? ` (${r.confirmations} تأیید)` : ""} | فاکتور: ${fmtToman(r.invoice_toman)}`
    : `${r.received_usdt} USDT${r.confirmations != null ? ` (${r.confirmations} تأیید)` : ""} | فاکتور: ${r.invoice_usdt} USDT`;
  const chainCheck = useMutation({
    mutationFn: (id: number) => depositCheck(id),
    onSuccess: (r: any) => {
      if (!r?.available) { show("واریزی از زنجیره خوانده نشد؛ از روی لینک تراکنش بررسی کنید.", "error"); return; }
      const m = r.match === true ? " — ✓ مطابق فاکتور" : r.match === false ? " — ✗ مغایر با فاکتور" : "";
      show(`واریزی: ${depLabel(r)}${m}`, r.match === false ? "error" : "success");
    },
    onError: (e) => show(errMsg(e), "error"),
  });
  const reject = useMutation({ mutationFn: rejectPayment, onSuccess: (r: any) => { show(r?.message || "رد شد"); refresh(); }, onError: (e) => show(errMsg(e), "error") });
  const confirm_ = useMutation({
    mutationFn: (id: number) => confirmPayment(id),
    onSuccess: (r: any) => { show(r?.message || "تأیید شد"); setConfirmRow(null); refresh(); },
    onError: (e) => show(errMsg(e), "error"),
  });

  const del = useMutation({ mutationFn: deletePayment, onSuccess: (r: any) => { show(r?.message || "حذف شد"); refresh(); }, onError: (e) => show(errMsg(e), "error") });

  const doReject = (p: any) => {
    const extra = p.status === "confirmed" ? "\n(این پرداخت تأییدشده بود؛ رد آن فاکتورهای تسویه‌شده را دوباره «پرداخت‌نشده» می‌کند.)" : "";
    if (window.confirm(`پرداخت «${p.reseller_name || ""}» رد شود؟${extra}`)) reject.mutate(p.id);
  };
  const doDelete = (p: any) => {
    const extra = p.status === "confirmed" ? "\n(این پرداخت تأییدشده بود؛ با حذف، فاکتورهای مرتبط دوباره «پرداخت‌نشده» می‌شوند.)" : "";
    const scope = p.invoice_count > 1 ? `${p.invoice_count} فاکتور` : `دوره ${p.invoice_period || "—"}`;
    if (window.confirm(`پرداختِ «${p.reseller_name || ""}» (${scope}) برای همیشه حذف شود؟${extra}`)) del.mutate(p.id);
  };

  return (
    <Box>
      <Stack direction={{ xs: "column", sm: "row" }} spacing={1.5} sx={{ mb: 2 }}>
        <Select
          size="small"
          value={status}
          displayEmpty
          onChange={(e) => setStatus(e.target.value)}
          renderValue={(v) => v ? PAYMENT_STATUS_FA[v] : "همه وضعیت‌ها"}
          sx={{ minWidth: 160, "& .MuiSelect-select": { py: "7px !important" } }}
        >
          <MenuItem value="">همه وضعیت‌ها</MenuItem>
          {Object.entries(PAYMENT_STATUS_FA).map(([k, v]) => <MenuItem key={k} value={k}>{v}</MenuItem>)}
        </Select>
        <TextField size="small" value={search} sx={{ minWidth: { sm: 240 } }}
          placeholder="جستجوی شماره یا نام نماینده..." onChange={(e) => setSearch(e.target.value)}
          InputProps={{ startAdornment: <InputAdornment position="start"><SearchIcon fontSize="small" /></InputAdornment> }} />
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
              <SortTh id="total_amount_toman" label="مبلغ" sortKey={key} dir={dir} onSort={toggle} />
              <SortTh id="confirmations" label="تأییدها" sortKey={key} dir={dir} onSort={toggle} />
              <SortTh id="status" label="وضعیت" sortKey={key} dir={dir} onSort={toggle} />
              <SortTh id="created_at" label="تاریخ" sortKey={key} dir={dir} onSort={toggle} />
              <TableCell align="left">عملیات</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {shown.map((p: any) => (
              <TableRow key={p.id} hover>
                <TableCell data-label="#" dir="ltr" sx={{ color: "text.secondary", fontWeight: 600, fontFamily: "monospace" }}>#{p.number}</TableCell>
                <TableCell data-label="نماینده">
                  {/* Click the name → open the customer's Telegram PV (username if known, else by id). */}
                  {p.reseller_username
                    ? <Tooltip title="باز کردن گفتگوی تلگرام"><Link href={`https://t.me/${p.reseller_username}`} target="_blank" rel="noopener" underline="hover">{p.reseller_name}</Link></Tooltip>
                    : p.reseller_chat_id
                      ? <Tooltip title="باز کردن گفتگوی تلگرام (با شناسهٔ عددی)"><Link href={`tg://user?id=${p.reseller_chat_id}`} underline="hover">{p.reseller_name}</Link></Tooltip>
                      : p.reseller_name}
                </TableCell>
                <TableCell data-label="فاکتور (دوره)">
                  {p.invoice_count > 1
                    ? <Tooltip title={<span style={{ whiteSpace: "pre-line" }}>{(p.invoices || []).map((iv: any) => `دورهٔ ${iv.period}: ${fmtToman(iv.amount_toman)}`).join("\n")}</span>}>
                        <span style={{ cursor: "help" }}>{p.invoice_count} فاکتور ({(p.invoices || []).map((iv: any) => iv.period).join("، ")})</span>
                      </Tooltip>
                    : (p.invoice_period || "—")}
                </TableCell>
                <TableCell data-label="روش">{PAYMENT_METHOD_FA[p.method] || p.method}</TableCell>
                <TableCell data-label="TXID" dir="ltr" sx={{ maxWidth: 180, overflow: "hidden", textOverflow: "ellipsis" }}>
                  {/* Click the hash → open it on the matching explorer (TON → tonscan, AVAX →
                      snowtrace, else bscscan) so the owner can verify it manually before confirming. */}
                  {p.txid
                    ? <Tooltip title="باز کردن در اکسپلورر برای بررسی"><Link href={p.chain === "ton" ? `https://tonscan.org/tx/${p.txid}` : p.chain === "avax" ? `https://snowtrace.io/tx/${p.txid}` : `https://bscscan.com/tx/${p.txid}`} target="_blank" rel="noopener">{p.txid.slice(0, 14)}…</Link></Tooltip>
                    : p.has_proof
                      ? <Tooltip title="مشاهدهٔ رسید"><IconButton size="small" onClick={() => openPaymentProof(p.id)}><ImageIcon fontSize="small" /></IconButton></Tooltip>
                      : "—"}
                </TableCell>
                <TableCell data-label="مبلغ" dir="ltr">
                  <Tooltip title={
                    <span style={{ whiteSpace: "pre-line" }}>
                      {p.invoice_count > 1
                        ? (p.invoices || []).map((iv: any) => `دورهٔ ${iv.period}: ${fmtToman(iv.amount_toman)}`).join("\n")
                        : `فاکتور: ${p.invoice_amount_toman ? fmtToman(p.invoice_amount_toman) : "—"}${p.invoice_equiv ? "\nمعادل: " + p.invoice_equiv : ""}`}
                    </span>
                  }>
                    <span style={{ cursor: "help" }}>
                      {p.total_amount_toman ? fmtToman(p.total_amount_toman) : "—"}
                    </span>
                  </Tooltip>
                </TableCell>
                <TableCell data-label="تأییدها">{p.confirmations}</TableCell>
                <TableCell data-label="وضعیت"><Chip size="small" color={COLOR[p.status]} label={PAYMENT_STATUS_FA[p.status]} /></TableCell>
                <TableCell data-label="تاریخ">{fmtDate(p.created_at)}</TableCell>
                <TableCell data-label="عملیات" align="left">
                  {/* Actions stay available for every status so a wrong choice is reversible. */}
                  {/* On-chain check (read-only, free): reads the actual deposit — TON via toncenter,
                      AVAX via a public Avalanche C-Chain RPC, USDT/BEP-20 via a public BSC RPC node —
                      and reports it for the manual decision (never auto-confirms). */}
                  <Tooltip title="بررسی واریزی روی زنجیره"><span><IconButton size="small" disabled={!p.txid || chainCheck.isPending} onClick={() => chainCheck.mutate(p.id)}><VerifiedIcon fontSize="small" /></IconButton></span></Tooltip>
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
            {confirmRow.invoice_count > 1 ? (
              <Box sx={{ mb: 1 }}>
                <Typography variant="body2">فاکتورها ({confirmRow.invoice_count}):</Typography>
                <Stack component="ul" sx={{ m: 0, pr: 2.5 }} spacing={0.2}>
                  {(confirmRow.invoices || []).map((iv: any) => (
                    <Typography key={iv.id} component="li" variant="body2">
                      دورهٔ {iv.period} — {fmtToman(iv.amount_toman)}
                    </Typography>
                  ))}
                </Stack>
                <Typography variant="body2" sx={{ mt: 0.5 }}>
                  مبلغ کل: <b>{fmtToman(confirmRow.total_amount_toman)}</b>
                </Typography>
              </Box>
            ) : (
              <Typography variant="body2" sx={{ mb: 1 }}>
                فاکتور دوره: <b>{confirmRow.invoice_period || "—"}</b>
                {confirmRow.invoice_amount_toman ? <> — {fmtToman(confirmRow.invoice_amount_toman)}</> : null}
              </Typography>
            )}
            {confirmRow.txid && (
              <Box sx={{ mb: 1, p: 1, borderRadius: 2, bgcolor: "action.hover" }}>
                {depChk.isFetching ? (
                  <Typography variant="caption" color="text.secondary">در حال خواندن واریزی از زنجیره…</Typography>
                ) : depChk.data?.available ? (
                  <Stack spacing={0.3}>
                    {depChk.data.kind === "ton" ? (
                      <>
                        <Typography variant="body2">
                          واریزی: <b dir="ltr">{depChk.data.received_ton} GRAM</b> ≈ <b>{fmtToman(depChk.data.received_toman)}</b>
                        </Typography>
                        <Typography variant="body2">فاکتور: <b>{fmtToman(depChk.data.invoice_toman)}</b></Typography>
                        {depChk.data.match === true && <Chip size="small" color="success" label={`✓ مطابق (±${depChk.data.tolerance_pct}٪)`} />}
                        {depChk.data.match === false && <Chip size="small" color="error" label={`✗ مغایر (خارج از ±${depChk.data.tolerance_pct}٪)`} />}
                      </>
                    ) : depChk.data.kind === "avax" ? (
                      <>
                        <Typography variant="body2">
                          واریزی: <b dir="ltr">{depChk.data.received_avax} AVAX</b> ≈ <b>{fmtToman(depChk.data.received_toman)}</b>
                          {depChk.data.confirmations != null ? <> — {depChk.data.confirmations} تأیید</> : null}
                        </Typography>
                        <Typography variant="body2">فاکتور: <b>{fmtToman(depChk.data.invoice_toman)}</b></Typography>
                        {depChk.data.match === true && <Chip size="small" color="success" label={`✓ مطابق (±${depChk.data.tolerance_pct}٪)`} />}
                        {depChk.data.match === false && <Chip size="small" color="error" label={`✗ مغایر (خارج از ±${depChk.data.tolerance_pct}٪)`} />}
                      </>
                    ) : (
                      <>
                        <Typography variant="body2">
                          واریزی: <b dir="ltr">{depChk.data.received_usdt} USDT</b>
                          {depChk.data.confirmations != null ? <> — {depChk.data.confirmations} تأیید</> : null}
                        </Typography>
                        <Typography variant="body2">فاکتور: <b dir="ltr">{depChk.data.invoice_usdt} USDT</b></Typography>
                        {depChk.data.match === true && <Chip size="small" color="success" label={`✓ مطابق (±${depChk.data.tolerance_usdt} USDT)`} />}
                        {depChk.data.match === false && <Chip size="small" color="error" label={`✗ مغایر (±${depChk.data.tolerance_usdt} USDT)`} />}
                      </>
                    )}
                  </Stack>
                ) : (
                  <Typography variant="caption" color="text.secondary">
                    واریزی از زنجیره خوانده نشد؛ مبلغ را با لینکِ تراکنش دستی بررسی کنید.
                  </Typography>
                )}
              </Box>
            )}
            <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
              {confirmRow.invoice_count > 1
                ? `با تأیید، همهٔ این ${confirmRow.invoice_count} فاکتور «پرداخت‌شده» می‌شوند.`
                : "با تأیید، فقط همین فاکتور «پرداخت‌شده» می‌شود."}
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
