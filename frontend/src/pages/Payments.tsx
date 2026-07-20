import { useState } from "react";
import {
  Box, Button, Card, Chip, Dialog, DialogActions, DialogContent, DialogTitle,
  IconButton, InputAdornment, MenuItem, Select, Stack, Table, TableBody, TableCell, TableContainer,
  TableHead, TableRow, TextField, Tooltip, Typography, Link, useMediaQuery,
} from "@mui/material";
import { useTheme } from "@mui/material/styles";
import DownloadIcon from "@mui/icons-material/esm/Download";
import SearchIcon from "@mui/icons-material/esm/Search";
import VerifiedIcon from "@mui/icons-material/esm/Verified";
import CheckIcon from "@mui/icons-material/esm/Check";
import CloseIcon from "@mui/icons-material/esm/Close";
import ImageIcon from "@mui/icons-material/esm/Image";
import DeleteOutlineIcon from "@mui/icons-material/esm/DeleteOutline";
import { useQuery } from "@tanstack/react-query";
import {
  listPayments, confirmPayment, rejectPayment, deletePayment, openPaymentProof,
  depositCheck,
} from "../api/client";
import { useToast } from "../components/Toast";
import { useDialogState } from "../hooks/useDialogState";
import { useToastMutation } from "../hooks/useToastMutation";
import { useSort, SortTh } from "../components/sortable";
import { DataState } from "../components/DataState";
import { fmtToman, fmtDate, fmtNum, PAYMENT_STATUS_FA, PAYMENT_METHOD_FA } from "../format";
import { nestedCardBg } from "../theme";
import RowActionsMenu, { RowActionIcons, RowAction } from "../components/RowActionsMenu";
import { useXsFullScreen } from "../responsive";
import { downloadCsv } from "../csv";
import { MONEY_KEYS } from "../queryKeys";

const COLOR: any = { pending: "warning", confirmed: "success", rejected: "error" };

// A confirm/reject/delete can flip an invoice paid↔owed, so refresh the dependent views too.
const DEPENDENT_KEYS = MONEY_KEYS;

export default function Payments() {
  const { node, show } = useToast();
  const theme = useTheme();
  const isMobile = useMediaQuery(theme.breakpoints.down("md"));
  const xsFull = useXsFullScreen();
  const [status, setStatus] = useState("");
  const [search, setSearch] = useState("");
  const { data = [], isLoading, isError, refetch } = useQuery({
    queryKey: ["payments", status],
    queryFn: () => listPayments({ status: status || undefined }),
  });
  const { sorted, key, dir, toggle } = useSort(data, "created_at", "desc");
  // Search by tracking number (the public «#N» the customer quotes) or reseller name.
  // Persian/Arabic digits are normalized to ASCII so a hand-typed «#۱۲» matches the code.
  const toAscii = (s: string) =>
    s.replace(/[۰-۹]/g, (d) => "۰۱۲۳۴۵۶۷۸۹".indexOf(d).toString())
     .replace(/[٠-٩]/g, (d) => "٠١٢٣٤٥٦٧٨٩".indexOf(d).toString());
  const q = toAscii(search.trim().replace(/^#/, "")).toLowerCase();
  const matchesSearch = (p: any) =>
    String(p.number || "").includes(q) || (p.reseller_name || "").toLowerCase().includes(q);
  const shown = q ? sorted.filter(matchesSearch) : sorted;

  // ---- confirm dialog: a payment is for ONE invoice; the owner just confirms it ----
  const confirmDlg = useDialogState<any>();
  const confirmRow = confirmDlg.data;

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
  // Each mixed Latin/number+unit run is wrapped in a First-Strong Isolate (U+2068…U+2069) so the
  // toast reads cleanly in the RTL layout (otherwise «AVAX», the digits, and the separators reorder
  // and jumble). Neutral separators («≈», «—») sit between isolates and follow the RTL flow.
  const iso = (v: string | number) => `⁨${v}⁩`;
  const depLabel = (r: any) => {
    const confs = r.confirmations != null ? ` ${iso(`(${r.confirmations} تأیید)`)}` : "";
    if (r.kind === "ton")
      return `${iso(`${r.received_ton} GRAM`)} ≈ ${iso(fmtToman(r.received_toman))} — فاکتور ${iso(fmtToman(r.invoice_toman))}`;
    if (r.kind === "avax")
      return `${iso(`${r.received_avax} AVAX`)} ≈ ${iso(fmtToman(r.received_toman))}${confs} — فاکتور ${iso(fmtToman(r.invoice_toman))}`;
    return `${iso(`${r.received_usdt} USDT`)}${confs} — فاکتور ${iso(`${r.invoice_usdt} USDT`)}`;
  };
  const chainCheck = useToastMutation({
    show,
    mutationFn: (id: number) => depositCheck(id),
    success: (r: any): [string, "error" | "success"] => {
      if (!r?.available) return ["واریزی از زنجیره خوانده نشد؛ از روی لینک تراکنش بررسی کنید.", "error"];
      const m = r.match === true ? " ✓ مطابق فاکتور" : r.match === false ? " ✗ مغایر با فاکتور" : "";
      return [`واریزی: ${depLabel(r)}${m}`, r.match === false ? "error" : "success"];
    },
  });
  const reject = useToastMutation({
    show,
    mutationFn: rejectPayment,
    success: (r: any) => r?.message || "پرداخت رد شد",
    invalidate: DEPENDENT_KEYS,
  });
  const confirm_ = useToastMutation({
    show,
    mutationFn: (id: number) => confirmPayment(id),
    success: (r: any) => r?.message || "پرداخت تأیید شد",
    onSuccess: () => confirmDlg.close(),
    invalidate: DEPENDENT_KEYS,
  });

  const del = useToastMutation({
    show,
    mutationFn: deletePayment,
    success: (r: any) => r?.message || "پرداخت حذف شد",
    invalidate: DEPENDENT_KEYS,
  });

  const doReject = (p: any) => {
    const extra = p.status === "confirmed" ? "\n(این پرداخت تأییدشده بود؛ رد آن فاکتورهای تسویه‌شده را دوباره «پرداخت‌نشده» می‌کند.)" : "";
    if (window.confirm(`پرداخت «${p.reseller_name || ""}» رد شود؟${extra}`)) reject.mutate(p.id);
  };
  const doDelete = (p: any) => {
    const extra = p.status === "confirmed" ? "\n(این پرداخت تأییدشده بود؛ با حذف، فاکتورهای مرتبط دوباره «پرداخت‌نشده» می‌شوند.)" : "";
    const scope = p.invoice_count > 1 ? `${p.invoice_count} فاکتور` : `دورهٔ ${p.invoice_period || "—"}`;
    if (window.confirm(`پرداختِ «${p.reseller_name || ""}» (${scope}) برای همیشه حذف شود؟${extra}`)) del.mutate(p.id);
  };

  // CSV export: re-fetch the FULL dataset (the page query uses the backend default of 200;
  // the cap is 2000) with the CURRENT status filter, apply the same client-side search,
  // and build the file in the browser.
  const exportCsv = useToastMutation({
    show,
    mutationFn: async () => {
      const rows = await listPayments({ status: status || undefined, limit: 2000 });
      return q ? rows.filter(matchesSearch) : rows;
    },
    success: (rows: any[]) => `${fmtNum(rows.length)} پرداخت در فایل CSV ذخیره شد`,
    onSuccess: (rows: any[]) => downloadCsv(
      "payments.csv",
      ["#", "نماینده", "روش", "مبلغ (تومان)", "زنجیره", "TXID", "وضعیت", "تاریخ", "فاکتور (دوره)"],
      rows.map((p: any) => [
        p.number, p.reseller_name,
        PAYMENT_METHOD_FA[p.method] || p.method,
        p.total_amount_toman ?? "",
        p.chain || "", p.txid || "",
        PAYMENT_STATUS_FA[p.status] || p.status,
        p.created_at ? fmtDate(p.created_at) : "",
        p.invoice_count > 1
          ? (p.invoices || []).map((iv: any) => iv.period).join("، ")
          : (p.invoice_period || ""),
      ]),
    ),
  });

  // ---- Row pieces shared verbatim by the desktop table cells and the mobile cards ----

  // Click the name → open the customer's Telegram PV (username if known, else by id).
  const nameNode = (p: any) =>
    p.reseller_username
      ? <Tooltip title="باز کردن گفت‌وگوی تلگرام"><Link href={`https://t.me/${p.reseller_username}`} target="_blank" rel="noopener" underline="hover">{p.reseller_name}</Link></Tooltip>
      : p.reseller_chat_id
        ? <Tooltip title="باز کردن گفت‌وگوی تلگرام (با شناسهٔ عددی)"><Link href={`tg://user?id=${p.reseller_chat_id}`} underline="hover">{p.reseller_name}</Link></Tooltip>
        : p.reseller_name;

  const periodNode = (p: any) =>
    p.invoice_count > 1
      ? <Tooltip title={<span style={{ whiteSpace: "pre-line" }}>{(p.invoices || []).map((iv: any) => `دورهٔ ${iv.period}: ${fmtToman(iv.amount_toman)}`).join("\n")}</span>}>
          <span style={{ cursor: "help" }}>{p.invoice_count} فاکتور ({(p.invoices || []).map((iv: any) => iv.period).join("، ")})</span>
        </Tooltip>
      : (p.invoice_period || "—");

  // Click the hash → open it on the matching explorer (TON → tonscan, AVAX →
  // snowtrace, else bscscan) so the owner can verify it manually before confirming.
  const txidNode = (p: any) =>
    p.txid
      ? <Tooltip title="باز کردن در اکسپلورر برای بررسی"><Link href={p.chain === "ton" ? `https://tonscan.org/tx/${p.txid}` : p.chain === "avax" ? `https://snowtrace.io/tx/${p.txid}` : `https://bscscan.com/tx/${p.txid}`} target="_blank" rel="noopener">{p.txid.slice(0, 14)}…</Link></Tooltip>
      : p.has_proof
        ? <Tooltip title="مشاهدهٔ رسید"><IconButton size="small" onClick={() => openPaymentProof(p.id)}><ImageIcon fontSize="small" /></IconButton></Tooltip>
        : "—";

  const amountNode = (p: any) => (
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
  );

  // Actions stay available for every status so a wrong choice is reversible. Primary (mobile
  // touch buttons): تأیید / رد. Overflow (⋮): on-chain check + حذف. On-chain check is a
  // read-only, free deposit lookup (TON via toncenter, AVAX/USDT via public RPC) for the manual
  // decision — never auto-confirms.
  const actionsFor = (p: any): RowAction[] => [
    { key: "confirm", label: p.status === "confirmed" ? "تأییدشده" : "تأیید", color: "success", icon: <CheckIcon fontSize="small" />, disabled: p.status === "confirmed", onClick: () => confirmDlg.openWith(p), primary: true },
    { key: "reject", label: p.status === "rejected" ? "ردشده" : "رد", color: "error", icon: <CloseIcon fontSize="small" />, disabled: p.status === "rejected", onClick: () => doReject(p), primary: true },
    { key: "chain", label: "بررسی واریزی روی زنجیره", icon: <VerifiedIcon fontSize="small" />, disabled: !p.txid || chainCheck.isPending, onClick: () => chainCheck.mutate(p.id) },
    { key: "delete", label: "حذف", tooltip: "حذف کامل (برای پاک‌سازی داده‌های آزمایشی)", icon: <DeleteOutlineIcon fontSize="small" />, disabled: del.isPending, onClick: () => doDelete(p) },
  ];

  return (
    <Box>
      <Stack direction={{ xs: "column", sm: "row" }} spacing={1.5} sx={{ mb: 2 }}>
        <Select
          size="small"
          value={status}
          displayEmpty
          onChange={(e) => setStatus(e.target.value)}
          renderValue={(v) => v ? PAYMENT_STATUS_FA[v] : "همهٔ وضعیت‌ها"}
          sx={{ minWidth: 160, "& .MuiSelect-select": { py: "7px !important" } }}
        >
          <MenuItem value="">همهٔ وضعیت‌ها</MenuItem>
          {Object.entries(PAYMENT_STATUS_FA).map(([k, v]) => <MenuItem key={k} value={k}>{v}</MenuItem>)}
        </Select>
        <TextField size="small" value={search} sx={{ minWidth: { sm: 240 } }}
          placeholder="جستجوی شماره یا نام نماینده…" onChange={(e) => setSearch(e.target.value)}
          InputProps={{ startAdornment: <InputAdornment position="start"><SearchIcon fontSize="small" /></InputAdornment> }} />
        <Button size="small" variant="outlined" startIcon={<DownloadIcon />}
          onClick={() => exportCsv.mutate()} disabled={exportCsv.isPending || data.length === 0}>
          خروجی CSV
        </Button>
      </Stack>
      <DataState isLoading={isLoading} isError={isError} onRetry={refetch}>
      <Card sx={{ overflow: "hidden" }}>
        {!isMobile ? (
        // Fixed (viewport-based) height, not maxHeight: the card fills the page even when there
        // are only a few payments (maxHeight lets it shrink to the rows, leaving a gap below).
        // Rows sit at the top; the table scrolls INTERNALLY and the page stays put.
        <TableContainer sx={{ height: { xs: "auto", md: "calc(100vh - 180px)" } }}>
        <Table size="small" stickyHeader className="resp-table" sx={{ minWidth: 1120 }}>
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
                <TableCell data-label="نماینده">{nameNode(p)}</TableCell>
                <TableCell data-label="فاکتور (دوره)">{periodNode(p)}</TableCell>
                <TableCell data-label="روش">{PAYMENT_METHOD_FA[p.method] || p.method}</TableCell>
                <TableCell data-label="TXID" dir="ltr" sx={{ maxWidth: 180, overflow: "hidden", textOverflow: "ellipsis" }}>{txidNode(p)}</TableCell>
                <TableCell data-label="مبلغ" dir="ltr">{amountNode(p)}</TableCell>
                <TableCell data-label="تأییدها">{p.confirmations}</TableCell>
                <TableCell data-label="وضعیت"><Chip size="small" color={COLOR[p.status]} label={PAYMENT_STATUS_FA[p.status]} /></TableCell>
                <TableCell data-label="تاریخ">{fmtDate(p.created_at)}</TableCell>
                <TableCell data-label="عملیات" align="left"><RowActionIcons actions={actionsFor(p)} /></TableCell>
              </TableRow>
            ))}
            {shown.length === 0 && <TableRow><TableCell colSpan={10} align="center" sx={{ py: 4, color: "text.secondary" }}>{q ? "پرداختی با این جستجو یافت نشد" : "پرداختی ثبت نشده است"}</TableCell></TableRow>}
          </TableBody>
        </Table>
        </TableContainer>
        ) : (
        // Mobile: the same rows/actions as the table, stacked as cards (resellers pattern).
        <Stack spacing={1.2} sx={{ p: 1.5 }}>
          {shown.map((p: any) => (
            <Box key={p.id} sx={{
              p: 1.5, borderRadius: 3, border: "1px solid", borderColor: "divider",
              bgcolor: nestedCardBg,
            }}>
              <Stack direction="row" alignItems="flex-start" justifyContent="space-between" spacing={1}>
                <Box sx={{ minWidth: 0 }}>
                  <Typography variant="body2" component="div" sx={{ fontWeight: 750 }} noWrap>{nameNode(p)}</Typography>
                  <Typography variant="caption" color="text.secondary" dir="ltr" sx={{ fontFamily: "monospace" }}>
                    #{p.number}
                  </Typography>
                </Box>
                <Chip size="small" color={COLOR[p.status]} label={PAYMENT_STATUS_FA[p.status]} />
              </Stack>
              <Box sx={{ mt: 1.2, display: "grid", gridTemplateColumns: "1fr 1fr", gap: 1 }}>
                <Box>
                  <Typography variant="caption" color="text.secondary">مبلغ</Typography>
                  <Typography variant="body2" component="div" sx={{ fontWeight: 700 }}>{amountNode(p)}</Typography>
                </Box>
                <Box>
                  <Typography variant="caption" color="text.secondary">روش</Typography>
                  <Typography variant="body2">{PAYMENT_METHOD_FA[p.method] || p.method}</Typography>
                </Box>
                <Box>
                  <Typography variant="caption" color="text.secondary">فاکتور (دوره)</Typography>
                  <Typography variant="body2" component="div">{periodNode(p)}</Typography>
                </Box>
                <Box>
                  <Typography variant="caption" color="text.secondary">تاریخ</Typography>
                  <Typography variant="body2">{fmtDate(p.created_at)}</Typography>
                </Box>
              </Box>
              <Stack direction="row" alignItems="center" justifyContent="space-between" spacing={1}
                sx={{ mt: 1.2, pt: 1, borderTop: 1, borderColor: "divider" }}>
                <Box dir="ltr" sx={{ minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {txidNode(p)}
                </Box>
                <Box sx={{ flexShrink: 0 }}><RowActionsMenu actions={actionsFor(p)} /></Box>
              </Stack>
            </Box>
          ))}
          {shown.length === 0 && (
            <Typography align="center" color="text.secondary" variant="body2" sx={{ py: 5 }}>
              {q ? "پرداختی با این جستجو یافت نشد" : "پرداختی ثبت نشده است"}
            </Typography>
          )}
        </Stack>
        )}
      </Card>
      </DataState>

      {/* A payment is for ONE invoice — just confirm it (view the receipt first if a screenshot). */}
      <Dialog open={confirmDlg.open} onClose={confirmDlg.close} fullWidth maxWidth="xs" fullScreen={xsFull}>
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
                دورهٔ فاکتور: <b>{confirmRow.invoice_period || "—"}</b>
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
            <Button onClick={confirmDlg.close}>انصراف</Button>
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
