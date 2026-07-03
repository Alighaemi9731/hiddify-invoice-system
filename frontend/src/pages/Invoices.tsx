import { useState, useEffect } from "react";
import {
  Box, Button, Card, Chip, Dialog, DialogActions, DialogContent, DialogTitle,
  IconButton, MenuItem, Select, Stack, Table, TableBody, TableCell, TableHead, TableRow,
  TextField, Tooltip, Typography, Divider, TablePagination, useMediaQuery,
} from "@mui/material";
import { useTheme } from "@mui/material/styles";
import DownloadIcon from "@mui/icons-material/esm/Download";
import SendIcon from "@mui/icons-material/esm/Send";
import PictureAsPdfIcon from "@mui/icons-material/esm/PictureAsPdf";
import CheckCircleIcon from "@mui/icons-material/esm/CheckCircle";
import UndoIcon from "@mui/icons-material/esm/Undo";
import EditIcon from "@mui/icons-material/esm/Edit";
import VisibilityIcon from "@mui/icons-material/esm/Visibility";
import ScheduleIcon from "@mui/icons-material/esm/Schedule";
import AutorenewIcon from "@mui/icons-material/esm/Autorenew";
import RestartAltIcon from "@mui/icons-material/esm/RestartAlt";
import ReceiptLongIcon from "@mui/icons-material/esm/ReceiptLong";
import PersonOffIcon from "@mui/icons-material/esm/PersonOff";
import SearchIcon from "@mui/icons-material/esm/Search";
import InputAdornment from "@mui/material/InputAdornment";
import SegmentedTabs from "../components/SegmentedTabs";
import TelegramLink from "../components/TelegramLink";
import { useQuery } from "@tanstack/react-query";
import {
  listInvoices, generateInvoices, sendInvoice, sendPeriod, markInvoicePaid,
  unmarkInvoicePaid, editInvoice, getInvoice, openInvoicePdf, getZeroInvoices, deferInvoice,
  discardDrafts, recomputeInvoice, revertInvoiceToDraft,
} from "../api/client";
import { useToast } from "../components/Toast";
import { useDialogState } from "../hooks/useDialogState";
import { useXsFullScreen } from "../responsive";
import { useToastMutation } from "../hooks/useToastMutation";
import { useSort, SortTh } from "../components/sortable";
import { currentPeriod } from "../components/StatCard";
import PeriodPicker from "../components/PeriodPicker";
import LiveRate from "../components/LiveRate";
import { fmtToman, fmtGb, fmtNum, fmtDate, INVOICE_STATUS_FA } from "../format";
import { nestedCardBg } from "../theme";
import RowActionsMenu, { RowActionIcons, RowAction } from "../components/RowActionsMenu";
import { downloadCsv } from "../csv";

const STATUS_COLOR: any = { draft: "default", sent: "info", paid: "success", overdue: "warning", enforced: "error", canceled: "default" };
// Delivered-but-unpaid states money is collected against (mirrors the backend state machine).
const OWED_STATES = ["sent", "overdue", "enforced"];

export default function Invoices() {
  const { node, show } = useToast();
  const theme = useTheme();
  const isMobile = useMediaQuery(theme.breakpoints.down("md"));
  const xsFull = useXsFullScreen();
  const [period, setPeriod] = useState(currentPeriod());
  const [status, setStatus] = useState("");
  const [search, setSearch] = useState("");
  const detailDlg = useDialogState<any>();
  const editDlg = useDialogState<any>();
  const deferDlg = useDialogState<any>();
  const detail = detailDlg.data;
  const editRow = editDlg.data;
  const deferRow = deferDlg.data;
  const [tab, setTab] = useState(0);

  const { data = [] } = useQuery({
    queryKey: ["invoices", period, status],
    queryFn: () => listInvoices({ period, status: status || undefined, limit: 1000 }),
  });
  const { data: zero = [] } = useQuery({
    queryKey: ["zero-invoices", period],
    queryFn: () => getZeroInvoices(period),
    enabled: tab === 1,
  });
  const { sorted, key, dir, toggle } = useSort(data, "amount_toman", "desc");
  // Search by reseller name or the public 8-digit invoice number. Persian/Arabic digits are
  // normalized to ASCII so a hand-typed «۱۲۳۴…» matches the number shown on each row.
  const toAscii = (s: string) =>
    s.replace(/[۰-۹]/g, (d) => "۰۱۲۳۴۵۶۷۸۹".indexOf(d).toString())
     .replace(/[٠-٩]/g, (d) => "٠١٢٣٤٥٦٧٨٩".indexOf(d).toString());
  const q = toAscii(search.trim()).toLowerCase();
  const matchesSearch = (i: any) =>
    String(i.number || "").includes(q) || (i.reseller_name || "").toLowerCase().includes(q);
  const filtered = q ? sorted.filter(matchesSearch) : sorted;
  // Paginate the (often hundreds of) rows so we never render the whole month at once.
  const [page, setPage] = useState(0);
  const [rowsPerPage, setRowsPerPage] = useState(50);
  useEffect(() => { setPage(0); }, [period, status, search, tab, key, dir]);
  const paged = filtered.slice(page * rowsPerPage, page * rowsPerPage + rowsPerPage);
  const mut = (fn: any, ok: any) =>
    useToastMutation({ show, mutationFn: fn, success: ok, invalidate: ["invoices"] });

  const gen = mut(
    () => generateInvoices({ period }),
    (r: any) => {
      const parts = [`${r.created} فاکتور جدید`];
      if (r.updated) parts.push(`${r.updated} پیش‌نویس بازمحاسبه`);
      if (r.skipped_existing) parts.push(`${r.skipped_existing} ارسال/پرداخت‌شده دست‌نخورده`);
      if (r.reconciled_zero) parts.push(`${r.reconciled_zero} پیش‌نویسِ صفرشده حذف شد`);
      let msg = `${parts.join(" • ")} (${fmtToman(r.total_amount_toman)})`;
      if (r.skipped_panels?.length) {
        msg += ` — ⚠️ ${r.skipped_panels.length} پنل به‌دلیل همگام‌سازی ناموفق فاکتور نشد: ${r.skipped_panels.join("، ")}`;
      }
      return msg;
    },
  );
  const discard = mut(
    () => discardDrafts(period),
    (r: any) => (r.discarded ? `${r.discarded} پیش‌نویس حذف شد` : "پیش‌نویسی برای حذف نبود"),
  );
  const sendAll = mut(() => sendPeriod(period), (r: any) => `ارسال: ${r.sent} موفق، ${r.unmatched || 0} بدون ربات، ${r.failed || 0} ناموفق`);
  const sendOne = mut((id: number) => sendInvoice(id), (r: any) => `ارسال: ${r.delivery_status}`);
  const pay = mut((id: number) => markInvoicePaid(id), "به‌عنوان پرداخت‌شده ثبت شد");
  const recompute = mut(
    (id: number) => recomputeInvoice(id),
    (r: any) => `بازمحاسبه شد: ${fmtGb(r.usage_gb)} — ${fmtToman(r.amount_toman)}` + (r.synced ? "" : " (همگام‌سازی پنل ناموفق بود؛ از دادهٔ قبلی محاسبه شد)"),
  );
  const unpay = mut((id: number) => unmarkInvoicePaid(id), "پرداخت لغو شد (بازگشت به وضعیت قبل)");
  const toDraft = mut((id: number) => revertInvoiceToDraft(id), "به پیش‌نویس بازگردانده شد");
  const saveDefer = useToastMutation({
    show,
    mutationFn: () => deferInvoice(deferRow.id, {
      deferred_until: deferRow.deferred_until || null, defer_note: deferRow.defer_note || "",
    }),
    success: () => (deferRow.deferred_until ? "مهلت ثبت شد" : "مهلت حذف شد"),
    onSuccess: () => deferDlg.close(),
    invalidate: ["invoices"],
  });
  const saveEdit = useToastMutation({
    show,
    mutationFn: (resend: boolean) => editInvoice(editRow.id, {
      usage_gb: Number(editRow.usage_gb), price_per_gb: Number(editRow.price_per_gb),
    }).then((r) => (resend ? sendInvoice(editRow.id).then(() => r) : r)),
    success: (_d, resend) => (resend ? "ویرایش و ارسال مجدد شد" : "فاکتور ویرایش شد"),
    onSuccess: () => editDlg.close(),
    invalidate: ["invoices"],
  });

  // CSV export: re-fetch the FULL filtered dataset (the page query caps at 1000; the
  // backend cap is 2000) with the CURRENT period/status filters, apply the same
  // client-side search, and build the file in the browser.
  const exportCsv = useToastMutation({
    show,
    mutationFn: async () => {
      const rows = await listInvoices({ period, status: status || undefined, limit: 2000 });
      return q ? rows.filter(matchesSearch) : rows;
    },
    success: (rows: any[]) => `${fmtNum(rows.length)} فاکتور در فایل CSV ذخیره شد`,
    onSuccess: (rows: any[]) => downloadCsv(
      `invoices-${period}.csv`,
      ["شماره", "نماینده", "پنل", "دوره", "گیگ", "مبلغ (تومان)", "وضعیت", "تاریخ ارسال", "تاریخ پرداخت"],
      rows.map((i: any) => [
        i.number, i.reseller_name, i.panel_key, i.period_label, i.usage_gb, i.amount_toman,
        INVOICE_STATUS_FA[i.status] || i.status,
        i.sent_at ? fmtDate(i.sent_at) : "",
        i.paid_at ? fmtDate(i.paid_at) : "",
      ]),
    ),
  });

  const openDetail = async (id: number) => detailDlg.openWith(await getInvoice(id));
  const total = filtered.reduce((sum, invoice) => sum + invoice.amount_toman, 0);

  // The per-row operations as a single RowAction[] — one source of truth rendered as an icon
  // row on desktop (RowActionIcons) and as 1–2 labeled buttons + a ⋮ menu on mobile
  // (RowActionsMenu). `primary` marks the touch-first buttons: «جزئیات» plus the one contextual
  // money action (ثبت پرداخت for owed, ارسال for a draft).
  const actionsFor = (i: any): RowAction[] => {
    const owed = OWED_STATES.includes(i.status);
    const acts: RowAction[] = [
      { key: "detail", label: "جزئیات", icon: <VisibilityIcon fontSize="small" />, onClick: () => openDetail(i.id), primary: true },
    ];
    if (owed) acts.push({ key: "pay", label: "ثبت پرداخت", color: "success", icon: <CheckCircleIcon fontSize="small" />, onClick: () => pay.mutate(i.id), primary: true });
    else if (i.status === "draft") acts.push({ key: "send", label: "ارسال", color: "primary", icon: <SendIcon fontSize="small" />, onClick: () => sendOne.mutate(i.id), primary: true });
    if (i.status !== "paid" && i.status !== "canceled")
      acts.push({ key: "edit", label: "ویرایش", icon: <EditIcon fontSize="small" />, onClick: () => editDlg.openWith({ ...i }) });
    if (i.status !== "paid")
      acts.push({ key: "recompute", label: "بازمحاسبه", tooltip: "بازمحاسبه از روی پنل (همگام‌سازی + به‌روزرسانی اعداد)", icon: <AutorenewIcon fontSize="small" />, disabled: recompute.isPending, onClick: () => confirm("پنل همگام‌سازی و اعداد این فاکتور از روی دادهٔ فعلی پنل به‌روز شود؟") && recompute.mutate(i.id) });
    acts.push({ key: "pdf", label: "PDF", icon: <PictureAsPdfIcon fontSize="small" />, onClick: () => openInvoicePdf(i.id).catch(() => show("خطا در دریافت PDF", "error")) });
    if (i.status !== "draft") acts.push({ key: "send", label: "ارسال مجدد", icon: <SendIcon fontSize="small" />, onClick: () => sendOne.mutate(i.id) });
    if (i.status !== "draft" && i.status !== "paid")
      acts.push({ key: "todraft", label: "بازگردانی به پیش‌نویس", tooltip: "بازگردانی به پیش‌نویس (برای آزمایش/اصلاح؛ از دفتر مالی هم حذف می‌شود)", color: "warning", icon: <RestartAltIcon fontSize="small" />, disabled: toDraft.isPending, onClick: () => confirm("این فاکتور به «پیش‌نویس» بازگردانده شود؟ (وضعیت ارسال پاک و از تاریخچهٔ مالی حذف می‌شود)") && toDraft.mutate(i.id) });
    if (i.status === "paid")
      acts.push({ key: "unpay", label: "لغو پرداخت", color: "warning", icon: <UndoIcon fontSize="small" />, onClick: () => unpay.mutate(i.id) });
    if (owed)
      acts.push({ key: "defer", label: i.deferred_until ? `مهلت تا ${i.deferred_until}` : "مهلت پرداخت", color: i.deferred_until ? "info" : undefined, icon: <ScheduleIcon fontSize="small" />, onClick: () => deferDlg.openWith({ id: i.id, deferred_until: i.deferred_until || "", defer_note: i.defer_note || "", name: i.reseller_name }) });
    return acts;
  };

  return (
    <Box>
      <Stack direction={{ xs: "column", md: "row" }} spacing={2} sx={{ mb: 2 }} alignItems="center">
        <PeriodPicker value={period} onChange={setPeriod} />
        <Select
          size="small"
          value={status}
          displayEmpty
          onChange={(e) => setStatus(e.target.value)}
          renderValue={(v) => v ? INVOICE_STATUS_FA[v] : "همه وضعیت‌ها"}
          sx={{ minWidth: 148, "& .MuiSelect-select": { py: "7px !important" } }}
        >
          <MenuItem value="">همه وضعیت‌ها</MenuItem>
          {Object.entries(INVOICE_STATUS_FA).map(([k, v]) => <MenuItem key={k} value={k}>{v}</MenuItem>)}
        </Select>
        <Button size="small" variant="outlined" startIcon={<DownloadIcon />}
          onClick={() => exportCsv.mutate()} disabled={exportCsv.isPending || data.length === 0}>
          خروجی CSV
        </Button>
        <Box sx={{ flexGrow: 1 }} />
        <LiveRate />
        <Button variant="outlined" onClick={() => {
          // Guard against billing an incomplete month: the normal action is to issue the
          // PREVIOUS (completed) month. Generating the current/future month would invoice only
          // the services created so far — confirm before proceeding.
          if (period >= currentPeriod() && !confirm(
            `دورهٔ ${period} هنوز تمام نشده؛ فاکتورِ این دوره ناقص می‌شود (فقط سرویس‌های ثبت‌شده تا امروز). معمولاً باید دورهٔ ماهِ گذشته را صادر کنید. ادامه می‌دهید؟`
          )) return;
          gen.mutate();
        }} disabled={gen.isPending}>صدور فاکتورهای دوره</Button>
        <Button variant="outlined" color="warning" onClick={() => {
          if (confirm(`همهٔ پیش‌نویس‌های دوره ${period} حذف شوند؟ (فاکتورهای ارسال/پرداخت‌شده دست‌نخورده می‌مانند)`)) discard.mutate();
        }} disabled={discard.isPending}>حذف پیش‌نویس‌ها</Button>
        <Button variant="contained" startIcon={<SendIcon />} disabled={sendAll.isPending}
          onClick={() => {
            if (confirm(`فاکتورهای پیش‌نویسِ دورهٔ ${period} برای همهٔ نماینده‌ها ارسال شوند؟ این عمل به همهٔ نماینده‌ها پیام می‌فرستد و قابلِ بازگشت نیست.`))
              sendAll.mutate();
          }}>ارسال همه پیش‌نویس‌ها</Button>
      </Stack>

      <Stack direction={{ xs: "column", md: "row" }} spacing={2} sx={{ mb: 2 }} alignItems={{ md: "center" }}>
        <SegmentedTabs
          value={tab}
          onChange={setTab}
          tabs={[
            { label: "فاکتورها", icon: <ReceiptLongIcon fontSize="small" /> },
            { label: "نمایندگان با فاکتور صفر", icon: <PersonOffIcon fontSize="small" /> },
          ]}
        />
        <Box sx={{ flexGrow: 1 }} />
        {tab === 0 && (
          <TextField
            size="small"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="جستجو: نام نماینده یا شماره فاکتور"
            sx={{ minWidth: { xs: "100%", md: 280 } }}
            InputProps={{
              startAdornment: (
                <InputAdornment position="start"><SearchIcon fontSize="small" /></InputAdornment>
              ),
            }}
          />
        )}
      </Stack>

      {tab === 1 ? (
        <Card>
          <Typography variant="body2" color="text.secondary" sx={{ p: 2, pb: 0 }}>
            {fmtNum(zero.length)} نماینده در دوره {period} هیچ فروشی نداشته‌اند.
          </Typography>
          <Table size="small" className="resp-table">
            <TableHead>
              <TableRow>
                <TableCell>نماینده</TableCell><TableCell align="center">تلگرام</TableCell><TableCell>پنل</TableCell>
                <TableCell>زیرمجموعه‌ها</TableCell><TableCell>ربات</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {zero.map((z: any) => (
                <TableRow key={z.reseller_id} hover>
                  <TableCell>{z.reseller_name}</TableCell>
                  <TableCell align="center"><TelegramLink username={z.reseller_username} chatId={z.reseller_chat_id} /></TableCell>
                  <TableCell>{z.panel_key}</TableCell>
                  <TableCell>{fmtNum(z.sub_resellers)}</TableCell>
                  <TableCell>{z.registered ? <Chip size="small" color="success" label="متصل" /> : <Chip size="small" label="—" />}</TableCell>
                </TableRow>
              ))}
              {zero.length === 0 && <TableRow><TableCell colSpan={5} align="center" sx={{ py: 4, color: "text.secondary" }}>همه نماینده‌ها در این دوره فروش داشته‌اند</TableCell></TableRow>}
            </TableBody>
          </Table>
        </Card>
      ) : (
      <>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
        {fmtNum(filtered.length)} فاکتور{q ? ` (از ${fmtNum(sorted.length)})` : ""} — جمع: {fmtToman(total)}
      </Typography>

      <Card>
        {!isMobile ? (
        <Table size="small" className="resp-table">
          <TableHead>
            <TableRow>
              <TableCell>شماره</TableCell>
              <SortTh id="reseller_name" label="نماینده" sortKey={key} dir={dir} onSort={toggle} />
              <TableCell align="center">تلگرام</TableCell>
              <SortTh id="panel_key" label="پنل" sortKey={key} dir={dir} onSort={toggle} />
              <SortTh id="usage_gb" label="مصرف" sortKey={key} dir={dir} onSort={toggle} />
              <SortTh id="amount_toman" label="مبلغ (تومان)" sortKey={key} dir={dir} onSort={toggle} />
              <SortTh id="status" label="وضعیت" sortKey={key} dir={dir} onSort={toggle} />
              <TableCell align="left">عملیات</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {paged.map((i: any) => (
              <TableRow key={i.id} hover>
                <TableCell sx={{ fontFamily: "monospace", whiteSpace: "nowrap" }} dir="ltr">{i.number}</TableCell>
                <TableCell>{i.reseller_name}</TableCell>
                <TableCell align="center"><TelegramLink username={i.reseller_username} chatId={i.reseller_chat_id} /></TableCell>
                <TableCell>{i.panel_key}</TableCell>
                <TableCell>{fmtGb(i.usage_gb)}</TableCell>
                <TableCell>{fmtToman(i.amount_toman)}</TableCell>
                <TableCell><Chip size="small" color={STATUS_COLOR[i.status]} label={INVOICE_STATUS_FA[i.status]} /></TableCell>
                <TableCell align="left" sx={{ whiteSpace: "nowrap" }}><RowActionIcons actions={actionsFor(i)} /></TableCell>
              </TableRow>
            ))}
            {filtered.length === 0 && <TableRow><TableCell colSpan={9} align="center" sx={{ py: 4, color: "text.secondary" }}>{q ? "نتیجه‌ای برای جستجو پیدا نشد" : "فاکتوری برای این دوره نیست — «صدور فاکتورهای دوره» را بزنید"}</TableCell></TableRow>}
          </TableBody>
        </Table>
        ) : (
        // Mobile: the same rows/actions as the table, stacked as cards (resellers pattern).
        <Stack spacing={1.2} sx={{ p: 1.5 }}>
          {paged.map((i: any) => (
            <Box key={i.id} sx={{
              p: 1.5, borderRadius: 3, border: "1px solid", borderColor: "divider",
              bgcolor: nestedCardBg,
            }}>
              <Stack direction="row" alignItems="flex-start" justifyContent="space-between" spacing={1}>
                <Box sx={{ minWidth: 0 }}>
                  <Typography variant="body2" sx={{ fontWeight: 750 }} noWrap>{i.reseller_name}</Typography>
                  <Typography variant="caption" color="text.secondary" dir="ltr" sx={{ fontFamily: "monospace" }}>
                    {i.number}
                  </Typography>
                </Box>
                <Chip size="small" color={STATUS_COLOR[i.status]} label={INVOICE_STATUS_FA[i.status]} />
              </Stack>
              <Stack direction="row" spacing={0.8} flexWrap="wrap" useFlexGap alignItems="center" sx={{ mt: 1 }}>
                <Chip size="small" variant="outlined" label={i.panel_key} />
                <Chip size="small" variant="outlined" label={`دوره ${i.period_label}`} />
                <TelegramLink username={i.reseller_username} chatId={i.reseller_chat_id} />
              </Stack>
              <Box sx={{ mt: 1.2, display: "grid", gridTemplateColumns: "1fr 1fr", gap: 1 }}>
                <Box>
                  <Typography variant="caption" color="text.secondary">مصرف</Typography>
                  <Typography variant="body2" sx={{ fontWeight: 700 }}>{fmtGb(i.usage_gb)}</Typography>
                </Box>
                <Box>
                  <Typography variant="caption" color="text.secondary">مبلغ</Typography>
                  <Typography variant="body2" sx={{ fontWeight: 700 }}>{fmtToman(i.amount_toman)}</Typography>
                </Box>
              </Box>
              <Box sx={{ mt: 1.2, pt: 1, borderTop: 1, borderColor: "divider" }}>
                <RowActionsMenu actions={actionsFor(i)} />
              </Box>
            </Box>
          ))}
          {filtered.length === 0 && (
            <Typography align="center" color="text.secondary" variant="body2" sx={{ py: 5 }}>
              {q ? "نتیجه‌ای برای جستجو پیدا نشد" : "فاکتوری برای این دوره نیست — «صدور فاکتورهای دوره» را بزنید"}
            </Typography>
          )}
        </Stack>
        )}
        {filtered.length > rowsPerPage && (
          <TablePagination
            component="div" count={filtered.length} page={page}
            onPageChange={(_, p) => setPage(p)}
            rowsPerPage={rowsPerPage} rowsPerPageOptions={[25, 50, 100]}
            onRowsPerPageChange={(e) => { setRowsPerPage(parseInt(e.target.value, 10)); setPage(0); }}
            labelRowsPerPage="تعداد در صفحه:"
            labelDisplayedRows={({ from, to, count }) => `${from}–${to} از ${count}`}
          />
        )}
      </Card>
      </>
      )}

      {/* Edit dialog */}
      <Dialog open={editDlg.open} onClose={editDlg.close} fullWidth maxWidth="xs" fullScreen={xsFull}>
        {editRow && (<>
          <DialogTitle>ویرایش فاکتور — {editRow.reseller_name}</DialogTitle>
          <DialogContent>
            <Stack spacing={2} sx={{ mt: 1 }}>
              <TextField label="مصرف (گیگ)" type="number" value={editRow.usage_gb}
                onChange={(e) => editDlg.setData({ ...editRow, usage_gb: e.target.value })} />
              <TextField label="قیمت هر گیگ (تومان)" type="number" value={editRow.price_per_gb}
                onChange={(e) => editDlg.setData({ ...editRow, price_per_gb: e.target.value })} />
              <Typography variant="body2" color="text.secondary">
                مبلغ جدید: {fmtToman(Number(editRow.usage_gb || 0) * Number(editRow.price_per_gb || 0))}
              </Typography>
            </Stack>
          </DialogContent>
          <DialogActions>
            <Button onClick={editDlg.close}>انصراف</Button>
            <Button onClick={() => saveEdit.mutate(false)} disabled={saveEdit.isPending}>ذخیره</Button>
            <Button variant="contained" onClick={() => saveEdit.mutate(true)} disabled={saveEdit.isPending}>ذخیره و ارسال مجدد</Button>
          </DialogActions>
        </>)}
      </Dialog>

      {/* Defer dialog */}
      <Dialog open={deferDlg.open} onClose={deferDlg.close} fullWidth maxWidth="xs" fullScreen={xsFull}>
        {deferRow && (<>
          <DialogTitle>مهلت پرداخت — {deferRow.name}</DialogTitle>
          <DialogContent>
            <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
              تا تاریخ انتخاب‌شده، یادآوری و مسدودسازی این فاکتور متوقف می‌شود. فاکتورهای دیگر و دادهٔ پنل تغییری نمی‌کنند.
            </Typography>
            <Stack spacing={2} sx={{ mt: 1 }}>
              <TextField type="date" label="مهلت تا" InputLabelProps={{ shrink: true }}
                value={deferRow.deferred_until || ""} onChange={(e) => deferDlg.setData({ ...deferRow, deferred_until: e.target.value })} />
              <TextField label="یادداشت (اختیاری)" value={deferRow.defer_note || ""}
                onChange={(e) => deferDlg.setData({ ...deferRow, defer_note: e.target.value })} />
            </Stack>
          </DialogContent>
          <DialogActions>
            <Button color="error" onClick={() => { deferDlg.setData({ ...deferRow, deferred_until: "" }); setTimeout(() => saveDefer.mutate(), 0); }}>حذف مهلت</Button>
            <Button onClick={deferDlg.close}>انصراف</Button>
            <Button variant="contained" onClick={() => saveDefer.mutate()} disabled={saveDefer.isPending || !deferRow.deferred_until}>ثبت مهلت</Button>
          </DialogActions>
        </>)}
      </Dialog>

      {/* Detail dialog */}
      <Dialog open={detailDlg.open} onClose={detailDlg.close} fullWidth maxWidth="md" fullScreen={xsFull}>
        {detail && (<>
          <DialogTitle>فاکتور #{detail.number} — {detail.reseller_name} — دوره {detail.period_label}</DialogTitle>
          <DialogContent>
            <Stack direction="row" spacing={3} sx={{ mb: 2, flexWrap: "wrap" }}>
              <Typography variant="body2">مصرف کل: <b>{fmtGb(detail.usage_gb)}</b></Typography>
              <Typography variant="body2">قیمت/گیگ: <b>{fmtNum(detail.price_per_gb)}</b></Typography>
              <Typography variant="body2">مبلغ: <b>{fmtToman(detail.amount_toman)}</b></Typography>
            </Stack>
            {detail.floor_applied && (
              <Chip size="small" color="warning" sx={{ mb: 2 }}
                label={`حداقل فروش اعمال شد (مبلغ واقعی مصرف: ${fmtToman(detail.base_amount_toman)})`} />
            )}
            <Divider sx={{ mb: 1 }} />
            <Typography variant="subtitle2" sx={{ mb: 1 }}>{fmtNum(detail.lines?.length)} سرویس</Typography>
            <Box sx={{ maxHeight: 360, overflow: "auto" }}>
              <Table size="small" stickyHeader className="resp-table">
                <TableHead><TableRow><TableCell>نام</TableCell><TableCell>تاریخ ساخت</TableCell><TableCell>حجم</TableCell></TableRow></TableHead>
                <TableBody>
                  {detail.lines?.map((l: any, idx: number) => (
                    <TableRow key={idx}><TableCell>{l.name}</TableCell><TableCell dir="ltr">{l.start_date}</TableCell><TableCell>{fmtGb(l.usage_gb)}</TableCell></TableRow>
                  ))}
                </TableBody>
              </Table>
            </Box>
          </DialogContent>
          <DialogActions>
            <Button onClick={() => openInvoicePdf(detail.id).catch(() => show("خطا در دریافت PDF", "error"))} startIcon={<PictureAsPdfIcon />}>دانلود PDF</Button>
            <Button onClick={detailDlg.close}>بستن</Button>
          </DialogActions>
        </>)}
      </Dialog>
      {node}
    </Box>
  );
}
