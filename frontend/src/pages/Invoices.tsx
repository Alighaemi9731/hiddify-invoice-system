import { useState, useEffect, useDeferredValue } from "react";
import {
  Autocomplete, Box, Button, Card, Checkbox, Chip, Dialog, DialogActions, DialogContent, DialogTitle,
  MenuItem, Select, Stack, Table, TableBody, TableCell, TableContainer, TableHead, TableRow,
  TextField, Typography, Divider, TablePagination, useMediaQuery,
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
import TelegramLink, { telegramHref } from "../components/TelegramLink";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  listInvoices, listInvoicesPaged, generateInvoices, sendInvoice, sendPeriod, markInvoicePaid,
  unmarkInvoicePaid, editInvoice, getInvoice, openInvoicePdf, getZeroInvoices, deferInvoice,
  listResellers, bulkDeferInvoices, discardDrafts, recomputeInvoice, revertInvoiceToDraft,
} from "../api/client";
import { useToast } from "../components/Toast";
import { useDialogState } from "../hooks/useDialogState";
import { useXsFullScreen } from "../responsive";
import { useToastMutation } from "../hooks/useToastMutation";
import { SortTh } from "../components/sortable";
import { MONEY_KEYS } from "../queryKeys";
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

  // "One reseller, all periods" mode: pick a reseller from the autocomplete → the list shows
  // EVERY invoice they have across every month (the backend list endpoint takes reseller_id
  // without a period), so you can act on all of them in one place. Null = the normal
  // period-scoped view.
  const [resellerFilter, setResellerFilter] = useState<{ id: number; name: string } | null>(null);
  const inResellerMode = !!resellerFilter;
  const [rInput, setRInput] = useState("");
  const rOptions = useQuery({
    queryKey: ["reseller-search", rInput],
    queryFn: () => listResellers({ q: rInput.trim() || undefined, limit: 30 }),
    enabled: rInput.trim().length >= 1,
  });

  // Server-side pagination (B4): the browser fetches ONE page; sorting, searching (by
  // reseller name or the public 8-digit invoice number — Persian digits normalized
  // server-side), filtering, count and money totals all happen in the database.
  const [page, setPage] = useState(0);
  const [rowsPerPage, setRowsPerPage] = useState(50);
  const [sortState, setSortState] = useState<{ key: string; dir: "asc" | "desc" }>(
    { key: "amount_toman", dir: "desc" },
  );
  const { key, dir } = sortState;
  const toggle = (k: string) =>
    setSortState((s) => (s.key === k ? { key: k, dir: s.dir === "asc" ? "desc" : "asc" } : { key: k, dir: "asc" }));
  const dq = useDeferredValue(search);
  const q = dq.trim();
  const listParams = (p: number) => ({
    ...(inResellerMode ? { reseller_id: resellerFilter!.id } : { period }),
    status: status || undefined,
    q: q || undefined,
    sort: key, order: dir, limit: rowsPerPage, offset: p * rowsPerPage,
  });
  const listKey = (p: number) =>
    ["invoices", inResellerMode ? `r${resellerFilter!.id}` : period, status, q, key, dir, rowsPerPage, p];
  const { data: pageData } = useQuery({
    queryKey: listKey(page),
    queryFn: ({ signal }) => listInvoicesPaged(listParams(page), signal),
  });
  const data = pageData?.rows ?? [];
  const totalCount = pageData?.total ?? 0;
  const totalAmount = pageData?.totalAmount ?? 0;
  // Prefetch the adjacent page so «بعدی» renders instantly.
  const qcPrefetch = useQueryClient();
  useEffect(() => {
    if ((page + 1) * rowsPerPage < totalCount)
      qcPrefetch.prefetchQuery({
        queryKey: listKey(page + 1),
        queryFn: ({ signal }) => listInvoicesPaged(listParams(page + 1), signal),
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pageData]);
  const { data: zero = [] } = useQuery({
    queryKey: ["zero-invoices", period],
    queryFn: () => getZeroInvoices(period),
    enabled: tab === 1,
  });
  // Row selection for bulk actions (extend deadline). Cleared whenever the data context changes
  // so a stale selection from another month/reseller can't leak into a bulk action.
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set());
  useEffect(() => { setPage(0); setSelectedIds(new Set()); }, [period, status, q, tab, key, dir, resellerFilter]);
  const paged = data;
  const pageIds = paged.map((i: any) => i.id);
  const allPageSelected = pageIds.length > 0 && pageIds.every((id) => selectedIds.has(id));
  const toggleId = (id: number) => setSelectedIds((prev) => {
    const n = new Set(prev); n.has(id) ? n.delete(id) : n.add(id); return n;
  });
  const toggleAllPage = () => setSelectedIds((prev) => {
    const n = new Set(prev);
    if (allPageSelected) pageIds.forEach((id) => n.delete(id));
    else pageIds.forEach((id) => n.add(id));
    return n;
  });
  const mut = (fn: any, ok: any) =>
    useToastMutation({ show, mutationFn: fn, success: ok, invalidate: ["invoices"] });
  // Money-changing mutations must also refresh Payments/Dashboard/Debts (a mark-paid/unpay
  // here flips state those views show) — the same dependent set Payments.tsx uses.
  const moneyMut = (fn: any, ok: any) =>
    useToastMutation({ show, mutationFn: fn, success: ok, invalidate: MONEY_KEYS });

  const gen = mut(
    () => generateInvoices({ period }),
    (r: any) => {
      const parts = [`${r.created} فاکتور جدید`];
      if (r.updated) parts.push(`${r.updated} پیش‌نویس بازمحاسبه‌شده`);
      if (r.skipped_existing) parts.push(`${r.skipped_existing} فاکتور ارسال/پرداخت‌شده بدون تغییر`);
      if (r.reconciled_zero) parts.push(`${r.reconciled_zero} پیش‌نویسِ صفرشده حذف شد`);
      let msg = `${parts.join(" • ")} (${fmtToman(r.total_amount_toman)})`;
      if (r.skipped_panels?.length) {
        msg += ` — ⚠️ برای ${r.skipped_panels.length} پنل به‌دلیل ناموفق‌بودن همگام‌سازی فاکتوری صادر نشد: ${r.skipped_panels.join("، ")}`;
      }
      return msg;
    },
  );
  const discard = mut(
    () => discardDrafts(period),
    (r: any) => (r.discarded ? `${r.discarded} پیش‌نویس حذف شد` : "پیش‌نویسی برای حذف وجود نداشت"),
  );
  const sendAll = mut(() => sendPeriod(period), (r: any) => `نتیجهٔ ارسال: ${r.sent} موفق، ${r.unmatched || 0} بدون اتصال به ربات، ${r.failed || 0} ناموفق`);
  const sendOne = mut((id: number) => sendInvoice(id), (r: any) => `وضعیت ارسال: ${r.delivery_status}`);
  const pay = moneyMut((id: number) => markInvoicePaid(id), "به‌عنوان پرداخت‌شده ثبت شد");
  const recompute = moneyMut(
    (id: number) => recomputeInvoice(id),
    (r: any) => `بازمحاسبه شد: ${fmtGb(r.usage_gb)} — ${fmtToman(r.amount_toman)}` + (r.synced ? "" : " (همگام‌سازی پنل ناموفق بود؛ از دادهٔ قبلی محاسبه شد)"),
  );
  const unpay = moneyMut((id: number) => unmarkInvoicePaid(id), "پرداخت لغو شد (بازگشت به وضعیت پیشین)");
  const toDraft = moneyMut((id: number) => revertInvoiceToDraft(id), "به پیش‌نویس بازگردانده شد");
  // Takes the deadline as an argument so «حذف مهلت» can pass "" directly instead of relying on
  // a setState + setTimeout(0) landing before the mutation reads it (fragile under concurrent
  // React scheduling — the old code could submit the STALE deadline). Money-relevant (a defer
  // pauses dunning), so use the dependent invalidation set.
  // Shared by the single «مهلت پرداخت» row action and the bulk «تمدید مهلت گروهی» — when
  // deferRow.ids is set it's the bulk path (several invoices at once).
  const saveDefer = useToastMutation<any, string>({
    show,
    mutationFn: (until: string) => deferRow?.ids
      ? bulkDeferInvoices({ ids: deferRow.ids, deferred_until: until || null, defer_note: deferRow.defer_note || "" })
      : deferInvoice(deferRow.id, { deferred_until: until || null, defer_note: deferRow.defer_note || "" }),
    success: (d: any, until) => {
      if (deferRow?.ids) {
        const skipped = d?.skipped?.length || 0;
        return `مهلتِ ${fmtNum(d?.done || 0)} فاکتور اعمال شد` +
          (skipped ? ` — ${fmtNum(skipped)} فاکتور رد شد (قابلِ تعیین مهلت نبود)` : "");
      }
      return until ? "مهلت ثبت شد" : "مهلت حذف شد";
    },
    onSuccess: () => { if (deferRow?.ids) setSelectedIds(new Set()); deferDlg.close(); },
    invalidate: MONEY_KEYS,
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

  // CSV export: re-fetch the FULL filtered dataset (server-filtered with the same
  // period/status/search the list uses; backend cap 2000) and build the file in the browser.
  const exportCsv = useToastMutation({
    show,
    mutationFn: () =>
      listInvoices({ period, status: status || undefined, q: q || undefined, limit: 2000 }),
    success: (rows: any[]) => `${fmtNum(rows.length)} فاکتور در فایل CSV ذخیره شد`,
    onSuccess: (rows: any[]) => downloadCsv(
      `invoices-${period}.csv`,
      ["شماره", "نماینده", "پنل", "دوره", "مصرف (گیگابایت)", "مبلغ (تومان)", "وضعیت", "تاریخ ارسال", "تاریخ پرداخت"],
      rows.map((i: any) => [
        i.number, i.reseller_name, i.panel_key, i.period_label, i.usage_gb, i.amount_toman,
        INVOICE_STATUS_FA[i.status] || i.status,
        i.sent_at ? fmtDate(i.sent_at) : "",
        i.paid_at ? fmtDate(i.paid_at) : "",
      ]),
    ),
  });

  const openDetail = async (id: number) => detailDlg.openWith(await getInvoice(id));

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
      acts.push({ key: "todraft", label: "بازگردانی به پیش‌نویس", tooltip: "بازگردانی به پیش‌نویس (برای آزمایش/اصلاح؛ از تاریخچهٔ مالی هم حذف می‌شود)", color: "warning", icon: <RestartAltIcon fontSize="small" />, disabled: toDraft.isPending, onClick: () => confirm("این فاکتور به «پیش‌نویس» بازگردانده شود؟ (وضعیت ارسال پاک و از تاریخچهٔ مالی حذف می‌شود)") && toDraft.mutate(i.id) });
    if (i.status === "paid")
      acts.push({ key: "unpay", label: "لغو پرداخت", color: "warning", icon: <UndoIcon fontSize="small" />, onClick: () => unpay.mutate(i.id) });
    if (owed)
      acts.push({ key: "defer", label: i.deferred_until ? `مهلت تا ${i.deferred_until}` : "مهلت پرداخت", color: i.deferred_until ? "info" : undefined, icon: <ScheduleIcon fontSize="small" />, onClick: () => deferDlg.openWith({ id: i.id, deferred_until: i.deferred_until || "", defer_note: i.defer_note || "", name: i.reseller_name }) });
    return acts;
  };

  return (
    <Box>
      <Stack direction={{ xs: "column", md: "row" }} spacing={2} sx={{ mb: 2 }} alignItems="center">
        {!inResellerMode && <PeriodPicker value={period} onChange={setPeriod} />}
        <Autocomplete
          size="small"
          sx={{ minWidth: { xs: "100%", md: 240 } }}
          options={rOptions.data || []}
          // Search is server-side (with relevance ordering); don't let MUI re-filter the
          // fetched options client-side (it would hide server matches whose label doesn't
          // literally contain the typed text, and fights the async update).
          filterOptions={(x) => x}
          getOptionLabel={(o: any) => o?.name || ""}
          renderOption={(props, o: any) => (
            <li {...props} key={o.id}>{o.name}{o.panel_key ? ` — ${o.panel_key}` : ""}</li>
          )}
          isOptionEqualToValue={(o: any, v: any) => o.id === v.id}
          value={resellerFilter as any}
          onChange={(_, v: any) => setResellerFilter(v ? { id: v.id, name: v.name } : null)}
          onInputChange={(_, v) => setRInput(v)}
          loading={rOptions.isFetching}
          noOptionsText={rInput.trim() ? "نماینده‌ای یافت نشد" : "نام نماینده را وارد کنید"}
          renderInput={(params) => (
            <TextField {...params} placeholder="فاکتورهای یک نماینده (همهٔ ماه‌ها)" />
          )}
        />
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
        <Button size="small" variant="outlined" startIcon={<DownloadIcon />}
          onClick={() => exportCsv.mutate()} disabled={exportCsv.isPending || data.length === 0}>
          خروجی CSV
        </Button>
        <Box sx={{ flexGrow: 1 }} />
        <LiveRate />
        {!inResellerMode && <>
          <Button variant="outlined" onClick={() => {
            // Guard against billing an incomplete month: the normal action is to issue the
            // PREVIOUS (completed) month. Generating the current/future month would invoice only
            // the services created so far — confirm before proceeding.
            if (period >= currentPeriod() && !confirm(
              `دورهٔ ${period} هنوز به پایان نرسیده است؛ فاکتورِ این دوره ناقص خواهد بود (فقط سرویس‌های ثبت‌شده تا امروز محاسبه می‌شود). توصیه می‌شود دورهٔ ماه گذشته را صادر کنید. ادامه می‌دهید؟`
            )) return;
            gen.mutate();
          }} disabled={gen.isPending}>صدور فاکتورهای دوره</Button>
          <Button variant="outlined" color="warning" onClick={() => {
            if (confirm(`همهٔ پیش‌نویس‌های دورهٔ ${period} حذف شوند؟ (فاکتورهای ارسال/پرداخت‌شده دست‌نخورده باقی می‌مانند)`)) discard.mutate();
          }} disabled={discard.isPending}>حذف پیش‌نویس‌ها</Button>
          <Button variant="contained" startIcon={<SendIcon />} disabled={sendAll.isPending}
            onClick={() => {
              if (confirm(`فاکتورهای پیش‌نویسِ دورهٔ ${period} برای همهٔ نماینده‌ها ارسال شوند؟ این عمل به همهٔ نماینده‌ها پیام می‌فرستد و قابلِ بازگشت نیست.`))
                sendAll.mutate();
            }}>ارسال همهٔ پیش‌نویس‌ها</Button>
        </>}
      </Stack>

      <Stack direction={{ xs: "column", md: "row" }} spacing={2} sx={{ mb: 2 }} alignItems={{ md: "center" }}>
        {inResellerMode ? (
          <Chip
            color="primary"
            onDelete={() => { setResellerFilter(null); setRInput(""); }}
            label={`همهٔ فاکتورهای «${resellerFilter!.name}» — ${fmtNum(totalCount)} فاکتور`}
          />
        ) : (
          <SegmentedTabs
            value={tab}
            onChange={setTab}
            tabs={[
              { label: "فاکتورها", icon: <ReceiptLongIcon fontSize="small" /> },
              { label: "نمایندگان با فاکتور صفر", icon: <PersonOffIcon fontSize="small" /> },
            ]}
          />
        )}
        <Box sx={{ flexGrow: 1 }} />
        {(inResellerMode || tab === 0) && (
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

      {(!inResellerMode && tab === 1) ? (
        <Card>
          <Typography variant="body2" color="text.secondary" sx={{ p: 2, pb: 0 }}>
            {fmtNum(zero.length)} نماینده در دورهٔ {period} هیچ فروشی نداشته‌اند.
          </Typography>
          <Table size="small" className="resp-table">
            <TableHead>
              <TableRow>
                <TableCell>نماینده</TableCell><TableCell>پنل</TableCell>
                <TableCell>زیرمجموعه‌ها</TableCell><TableCell>ربات</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {zero.map((z: any) => (
                <TableRow key={z.reseller_id} hover>
                  <TableCell>{z.reseller_name}</TableCell>
                  <TableCell>{z.panel_key}</TableCell>
                  <TableCell>{fmtNum(z.sub_resellers)}</TableCell>
                  <TableCell>
                    <Stack direction="row" spacing={0.5} alignItems="center">
                      {z.registered ? <Chip size="small" color="success" label="متصل" /> : <Chip size="small" label="—" />}
                      {telegramHref(z.reseller_username, z.reseller_chat_id) && (
                        <TelegramLink username={z.reseller_username} chatId={z.reseller_chat_id} />
                      )}
                    </Stack>
                  </TableCell>
                </TableRow>
              ))}
              {zero.length === 0 && <TableRow><TableCell colSpan={4} align="center" sx={{ py: 4, color: "text.secondary" }}>همهٔ نماینده‌ها در این دوره فروش داشته‌اند</TableCell></TableRow>}
            </TableBody>
          </Table>
        </Card>
      ) : (
      <>
      {selectedIds.size > 0 ? (
        <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 1 }}>
          <Chip color="primary" label={`${fmtNum(selectedIds.size)} فاکتور انتخاب شد`}
            onDelete={() => setSelectedIds(new Set())} />
          <Button size="small" variant="contained" startIcon={<ScheduleIcon fontSize="small" />}
            onClick={() => deferDlg.openWith({ ids: [...selectedIds], deferred_until: "", defer_note: "" })}>
            تمدید مهلت گروهی
          </Button>
        </Stack>
      ) : (
        <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
          {fmtNum(totalCount)} فاکتور — مجموع: {fmtToman(totalAmount)}
        </Typography>
      )}

      <Card>
        {!isMobile ? (
        <TableContainer sx={{ maxHeight: "calc(100vh - 300px)" }}>
        <Table size="small" stickyHeader className="resp-table" sx={{ minWidth: 980 }}>
          <TableHead>
            <TableRow>
              <TableCell padding="checkbox">
                <Checkbox size="small" checked={allPageSelected}
                  indeterminate={!allPageSelected && pageIds.some((id) => selectedIds.has(id))}
                  onChange={toggleAllPage} />
              </TableCell>
              <TableCell>شماره</TableCell>
              <SortTh id="reseller_name" label="نماینده" sortKey={key} dir={dir} onSort={toggle} />
              <TableCell align="center">تلگرام</TableCell>
              <SortTh id="panel_key" label="پنل" sortKey={key} dir={dir} onSort={toggle} />
              {inResellerMode && <SortTh id="period_label" label="دوره" sortKey={key} dir={dir} onSort={toggle} />}
              <SortTh id="usage_gb" label="مصرف" sortKey={key} dir={dir} onSort={toggle} />
              <SortTh id="amount_toman" label="مبلغ (تومان)" sortKey={key} dir={dir} onSort={toggle} />
              <SortTh id="status" label="وضعیت" sortKey={key} dir={dir} onSort={toggle} />
              <TableCell align="left">عملیات</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {paged.map((i: any) => (
              <TableRow key={i.id} hover selected={selectedIds.has(i.id)}>
                <TableCell padding="checkbox">
                  <Checkbox size="small" checked={selectedIds.has(i.id)} onChange={() => toggleId(i.id)} />
                </TableCell>
                <TableCell sx={{ fontFamily: "monospace", whiteSpace: "nowrap" }} dir="ltr">{i.number}</TableCell>
                <TableCell>{i.reseller_name}</TableCell>
                <TableCell align="center"><TelegramLink username={i.reseller_username} chatId={i.reseller_chat_id} /></TableCell>
                <TableCell>{i.panel_key}</TableCell>
                {inResellerMode && <TableCell dir="ltr">{i.period_label}</TableCell>}
                <TableCell>{fmtGb(i.usage_gb)}</TableCell>
                <TableCell>{fmtToman(i.amount_toman)}</TableCell>
                <TableCell><Chip size="small" color={STATUS_COLOR[i.status]} label={INVOICE_STATUS_FA[i.status]} /></TableCell>
                <TableCell align="left" sx={{ whiteSpace: "nowrap" }}><RowActionIcons actions={actionsFor(i)} /></TableCell>
              </TableRow>
            ))}
            {data.length === 0 && <TableRow><TableCell colSpan={inResellerMode ? 10 : 9} align="center" sx={{ py: 4, color: "text.secondary" }}>{q ? "نتیجه‌ای برای این جستجو یافت نشد" : inResellerMode ? "این نماینده هیچ فاکتوری ندارد" : "برای این دوره فاکتوری وجود ندارد — «صدور فاکتورهای دوره» را انتخاب کنید"}</TableCell></TableRow>}
          </TableBody>
        </Table>
        </TableContainer>
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
                <Chip size="small" variant="outlined" label={`دورهٔ ${i.period_label}`} />
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
          {data.length === 0 && (
            <Typography align="center" color="text.secondary" variant="body2" sx={{ py: 5 }}>
              {q ? "نتیجه‌ای برای این جستجو یافت نشد" : "برای این دوره فاکتوری وجود ندارد — «صدور فاکتورهای دوره» را انتخاب کنید"}
            </Typography>
          )}
        </Stack>
        )}
        {totalCount > rowsPerPage && (
          <TablePagination
            component="div" count={totalCount} page={page}
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
              <TextField label="مصرف (گیگابایت)" type="number" value={editRow.usage_gb}
                onChange={(e) => editDlg.setData({ ...editRow, usage_gb: e.target.value })} />
              <TextField label="قیمت هر گیگابایت (تومان)" type="number" value={editRow.price_per_gb}
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
          <DialogTitle>
            {deferRow.ids ? `تمدید مهلت گروهی — ${fmtNum(deferRow.ids.length)} فاکتور` : `مهلت پرداخت — ${deferRow.name}`}
          </DialogTitle>
          <DialogContent>
            <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
              {deferRow.ids
                ? "تا تاریخ انتخاب‌شده، یادآوری و مسدودسازیِ همهٔ فاکتورهای انتخاب‌شده متوقف می‌شود. فاکتورهایی که قابلِ تعیین مهلت نیستند (پیش‌نویس/پرداخت‌شده/لغوشده) رد می‌شوند."
                : "تا تاریخ انتخاب‌شده، یادآوری و مسدودسازی این فاکتور متوقف می‌شود. فاکتورهای دیگر و دادهٔ پنل تغییری نمی‌کنند."}
            </Typography>
            <Stack spacing={2} sx={{ mt: 1 }}>
              <TextField type="date" label="مهلت تا" InputLabelProps={{ shrink: true }}
                value={deferRow.deferred_until || ""} onChange={(e) => deferDlg.setData({ ...deferRow, deferred_until: e.target.value })} />
              <TextField label="یادداشت (اختیاری)" value={deferRow.defer_note || ""}
                onChange={(e) => deferDlg.setData({ ...deferRow, defer_note: e.target.value })} />
            </Stack>
          </DialogContent>
          <DialogActions>
            <Button color="error" onClick={() => saveDefer.mutate("")}>حذف مهلت</Button>
            <Button onClick={deferDlg.close}>انصراف</Button>
            <Button variant="contained" onClick={() => saveDefer.mutate(deferRow.deferred_until)} disabled={saveDefer.isPending || !deferRow.deferred_until}>ثبت مهلت</Button>
          </DialogActions>
        </>)}
      </Dialog>

      {/* Detail dialog */}
      <Dialog open={detailDlg.open} onClose={detailDlg.close} fullWidth maxWidth="md" fullScreen={xsFull}>
        {detail && (<>
          <DialogTitle>فاکتور #{detail.number} — {detail.reseller_name} — دورهٔ {detail.period_label}</DialogTitle>
          <DialogContent sx={{ display: "flex", flexDirection: "column" }}>
            <Stack direction="row" spacing={3} sx={{ mb: 2, flexWrap: "wrap", flexShrink: 0 }}>
              <Typography variant="body2">مصرف کل: <b>{fmtGb(detail.usage_gb)}</b></Typography>
              <Typography variant="body2">قیمت هر گیگابایت: <b>{fmtNum(detail.price_per_gb)}</b></Typography>
              <Typography variant="body2">مبلغ: <b>{fmtToman(detail.amount_toman)}</b></Typography>
            </Stack>
            {detail.floor_applied && (
              <Chip size="small" color="warning" sx={{ mb: 2 }}
                label={`حداقل فروش اعمال شد (مبلغ واقعی مصرف: ${fmtToman(detail.base_amount_toman)})`} />
            )}
            <Divider sx={{ mb: 1 }} />
            <Typography variant="subtitle2" sx={{ mb: 1 }}>{fmtNum(detail.lines?.length)} سرویس</Typography>
            {/* Fill the remaining space on a full-screen mobile dialog (so the list runs to the
                bottom instead of a short 360px box with empty space below); cap it on desktop. */}
            <Box sx={{ overflow: "auto", flex: { xs: 1, sm: "unset" }, minHeight: 0, maxHeight: { xs: "none", sm: 360 } }}>
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
