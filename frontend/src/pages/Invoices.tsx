import { useState, useEffect } from "react";
import {
  Box, Button, Card, Chip, Dialog, DialogActions, DialogContent, DialogTitle,
  IconButton, MenuItem, Stack, Table, TableBody, TableCell, TableHead, TableRow,
  TextField, Tooltip, Typography, Divider, Tabs, Tab, TablePagination,
} from "@mui/material";
import SendIcon from "@mui/icons-material/Send";
import PictureAsPdfIcon from "@mui/icons-material/PictureAsPdf";
import CheckCircleIcon from "@mui/icons-material/CheckCircle";
import UndoIcon from "@mui/icons-material/Undo";
import EditIcon from "@mui/icons-material/Edit";
import VisibilityIcon from "@mui/icons-material/Visibility";
import ScheduleIcon from "@mui/icons-material/Schedule";
import AutorenewIcon from "@mui/icons-material/Autorenew";
import RestartAltIcon from "@mui/icons-material/RestartAlt";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  listInvoices, generateInvoices, sendInvoice, sendPeriod, markInvoicePaid,
  unmarkInvoicePaid, editInvoice, getInvoice, openInvoicePdf, getZeroInvoices, deferInvoice,
  discardDrafts, recomputeInvoice, revertInvoiceToDraft,
} from "../api/client";
import { useToast, errMsg } from "../components/Toast";
import { useSort, SortTh } from "../components/sortable";
import { currentPeriod } from "../components/StatCard";
import PeriodPicker from "../components/PeriodPicker";
import { fmtToman, fmtGb, fmtNum, INVOICE_STATUS_FA } from "../format";

const STATUS_COLOR: any = { draft: "default", sent: "info", paid: "success", overdue: "warning", enforced: "error", canceled: "default" };

export default function Invoices() {
  const qc = useQueryClient();
  const { node, show } = useToast();
  const [period, setPeriod] = useState(currentPeriod());
  const [status, setStatus] = useState("");
  const [detail, setDetail] = useState<any>(null);
  const [editRow, setEditRow] = useState<any>(null);
  const [deferRow, setDeferRow] = useState<any>(null);
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
  // Paginate the (often hundreds of) rows so we never render the whole month at once.
  const [page, setPage] = useState(0);
  const [rowsPerPage, setRowsPerPage] = useState(50);
  useEffect(() => { setPage(0); }, [period, status, tab, key, dir]);
  const paged = sorted.slice(page * rowsPerPage, page * rowsPerPage + rowsPerPage);
  const refresh = () => qc.invalidateQueries({ queryKey: ["invoices"] });
  const mut = (fn: any, ok: any) =>
    useMutation({ mutationFn: fn, onSuccess: (r: any) => { show(typeof ok === "function" ? ok(r) : ok); refresh(); }, onError: (e) => show(errMsg(e), "error") });

  const gen = mut(
    () => generateInvoices({ period }),
    (r: any) => {
      const parts = [`${r.created} فاکتور جدید`];
      if (r.updated) parts.push(`${r.updated} پیش‌نویس بازمحاسبه`);
      if (r.skipped_existing) parts.push(`${r.skipped_existing} ارسال/پرداخت‌شده دست‌نخورده`);
      return `${parts.join(" • ")} (${fmtToman(r.total_amount_toman)})`;
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
  const saveDefer = useMutation({
    mutationFn: () => deferInvoice(deferRow.id, {
      deferred_until: deferRow.deferred_until || null, defer_note: deferRow.defer_note || "",
    }),
    onSuccess: () => { show(deferRow.deferred_until ? "مهلت ثبت شد" : "مهلت حذف شد"); setDeferRow(null); refresh(); },
    onError: (e) => show(errMsg(e), "error"),
  });
  const saveEdit = useMutation({
    mutationFn: (resend: boolean) => editInvoice(editRow.id, {
      usage_gb: Number(editRow.usage_gb), price_per_gb: Number(editRow.price_per_gb),
    }).then((r) => (resend ? sendInvoice(editRow.id).then(() => r) : r)),
    onSuccess: (_d, resend) => { show(resend ? "ویرایش و ارسال مجدد شد" : "فاکتور ویرایش شد"); setEditRow(null); refresh(); },
    onError: (e) => show(errMsg(e), "error"),
  });

  const openDetail = async (id: number) => setDetail(await getInvoice(id));
  const total = sorted.reduce((s: number, i: any) => s + i.amount_toman, 0);

  return (
    <Box>
      <Stack direction={{ xs: "column", md: "row" }} spacing={2} sx={{ mb: 2 }} alignItems="center">
        <PeriodPicker value={period} onChange={setPeriod} />
        <TextField select size="small" label="وضعیت" value={status} sx={{ minWidth: 140 }} onChange={(e) => setStatus(e.target.value)}>
          <MenuItem value="">همه</MenuItem>
          {Object.entries(INVOICE_STATUS_FA).map(([k, v]) => <MenuItem key={k} value={k}>{v}</MenuItem>)}
        </TextField>
        <Box sx={{ flexGrow: 1 }} />
        <Button variant="outlined" onClick={() => gen.mutate()} disabled={gen.isPending}>صدور فاکتورهای دوره</Button>
        <Button variant="outlined" color="warning" onClick={() => {
          if (confirm(`همهٔ پیش‌نویس‌های دوره ${period} حذف شوند؟ (فاکتورهای ارسال/پرداخت‌شده دست‌نخورده می‌مانند)`)) discard.mutate();
        }} disabled={discard.isPending}>حذف پیش‌نویس‌ها</Button>
        <Button variant="contained" startIcon={<SendIcon />} onClick={() => sendAll.mutate()} disabled={sendAll.isPending}>ارسال همه پیش‌نویس‌ها</Button>
      </Stack>

      <Tabs value={tab} onChange={(_, v) => setTab(v)} sx={{ mb: 2 }}>
        <Tab label="فاکتورها" />
        <Tab label="نمایندگان با فاکتور صفر" />
      </Tabs>

      {tab === 1 ? (
        <Card>
          <Typography variant="body2" color="text.secondary" sx={{ p: 2, pb: 0 }}>
            {fmtNum(zero.length)} نماینده در دوره {period} هیچ فروشی نداشته‌اند.
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
                  <TableCell>{z.registered ? <Chip size="small" color="success" label="متصل" /> : <Chip size="small" label="—" />}</TableCell>
                </TableRow>
              ))}
              {zero.length === 0 && <TableRow><TableCell colSpan={4} align="center" sx={{ py: 4, color: "text.secondary" }}>همه نماینده‌ها در این دوره فروش داشته‌اند</TableCell></TableRow>}
            </TableBody>
          </Table>
        </Card>
      ) : (
      <>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
        {fmtNum(sorted.length)} فاکتور — جمع: {fmtToman(total)}
      </Typography>

      <Card>
        <Table size="small" className="resp-table">
          <TableHead>
            <TableRow>
              <SortTh id="reseller_name" label="نماینده" sortKey={key} dir={dir} onSort={toggle} />
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
                <TableCell>{i.reseller_name}</TableCell>
                <TableCell>{i.panel_key}</TableCell>
                <TableCell>{fmtGb(i.usage_gb)}</TableCell>
                <TableCell>{fmtToman(i.amount_toman)}</TableCell>
                <TableCell><Chip size="small" color={STATUS_COLOR[i.status]} label={INVOICE_STATUS_FA[i.status]} /></TableCell>
                <TableCell align="left" sx={{ whiteSpace: "nowrap" }}>
                  <Tooltip title="جزئیات"><IconButton size="small" onClick={() => openDetail(i.id)}><VisibilityIcon fontSize="small" /></IconButton></Tooltip>
                  <Tooltip title="ویرایش"><IconButton size="small" onClick={() => setEditRow({ ...i })}><EditIcon fontSize="small" /></IconButton></Tooltip>
                  {i.status !== "paid" && (
                    <Tooltip title="بازمحاسبه از روی پنل (همگام‌سازی + به‌روزرسانی اعداد)">
                      <IconButton size="small" disabled={recompute.isPending}
                        onClick={() => confirm("پنل همگام‌سازی و اعداد این فاکتور از روی دادهٔ فعلی پنل به‌روز شود؟") && recompute.mutate(i.id)}>
                        <AutorenewIcon fontSize="small" />
                      </IconButton>
                    </Tooltip>
                  )}
                  <Tooltip title="PDF"><IconButton size="small" onClick={() => openInvoicePdf(i.id).catch(() => show("خطا در دریافت PDF", "error"))}><PictureAsPdfIcon fontSize="small" /></IconButton></Tooltip>
                  <Tooltip title="ارسال"><IconButton size="small" onClick={() => sendOne.mutate(i.id)}><SendIcon fontSize="small" /></IconButton></Tooltip>
                  {i.status !== "draft" && i.status !== "paid" && (
                    <Tooltip title="بازگردانی به پیش‌نویس (برای آزمایش/اصلاح؛ از دفتر مالی هم حذف می‌شود)">
                      <IconButton size="small" color="warning" disabled={toDraft.isPending}
                        onClick={() => confirm("این فاکتور به «پیش‌نویس» بازگردانده شود؟ (وضعیت ارسال پاک و از تاریخچهٔ مالی حذف می‌شود)") && toDraft.mutate(i.id)}>
                        <RestartAltIcon fontSize="small" />
                      </IconButton>
                    </Tooltip>
                  )}
                  {i.status === "paid" ? (
                    <Tooltip title="لغو پرداخت"><IconButton size="small" color="warning" onClick={() => unpay.mutate(i.id)}><UndoIcon fontSize="small" /></IconButton></Tooltip>
                  ) : (
                    <Tooltip title="ثبت پرداخت"><IconButton size="small" color="success" onClick={() => pay.mutate(i.id)}><CheckCircleIcon fontSize="small" /></IconButton></Tooltip>
                  )}
                  <Tooltip title={i.deferred_until ? `مهلت تا ${i.deferred_until}` : "مهلت پرداخت"}>
                    <IconButton size="small" color={i.deferred_until ? "info" : "default"}
                      onClick={() => setDeferRow({ id: i.id, deferred_until: i.deferred_until || "", defer_note: i.defer_note || "", name: i.reseller_name })}>
                      <ScheduleIcon fontSize="small" />
                    </IconButton>
                  </Tooltip>
                </TableCell>
              </TableRow>
            ))}
            {sorted.length === 0 && <TableRow><TableCell colSpan={7} align="center" sx={{ py: 4, color: "text.secondary" }}>فاکتوری برای این دوره نیست — «صدور فاکتورهای دوره» را بزنید</TableCell></TableRow>}
          </TableBody>
        </Table>
        {sorted.length > rowsPerPage && (
          <TablePagination
            component="div" count={sorted.length} page={page}
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
      <Dialog open={!!editRow} onClose={() => setEditRow(null)} fullWidth maxWidth="xs">
        {editRow && (<>
          <DialogTitle>ویرایش فاکتور — {editRow.reseller_name}</DialogTitle>
          <DialogContent>
            <Stack spacing={2} sx={{ mt: 1 }}>
              <TextField label="مصرف (گیگ)" type="number" value={editRow.usage_gb}
                onChange={(e) => setEditRow({ ...editRow, usage_gb: e.target.value })} />
              <TextField label="قیمت هر گیگ (تومان)" type="number" value={editRow.price_per_gb}
                onChange={(e) => setEditRow({ ...editRow, price_per_gb: e.target.value })} />
              <Typography variant="body2" color="text.secondary">
                مبلغ جدید: {fmtToman(Number(editRow.usage_gb || 0) * Number(editRow.price_per_gb || 0))}
              </Typography>
            </Stack>
          </DialogContent>
          <DialogActions>
            <Button onClick={() => setEditRow(null)}>انصراف</Button>
            <Button onClick={() => saveEdit.mutate(false)} disabled={saveEdit.isPending}>ذخیره</Button>
            <Button variant="contained" onClick={() => saveEdit.mutate(true)} disabled={saveEdit.isPending}>ذخیره و ارسال مجدد</Button>
          </DialogActions>
        </>)}
      </Dialog>

      {/* Defer dialog */}
      <Dialog open={!!deferRow} onClose={() => setDeferRow(null)} fullWidth maxWidth="xs">
        {deferRow && (<>
          <DialogTitle>مهلت پرداخت — {deferRow.name}</DialogTitle>
          <DialogContent>
            <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
              تا تاریخ انتخاب‌شده، یادآوری و مسدودسازی این فاکتور متوقف می‌شود. فاکتورهای دیگر و دادهٔ پنل تغییری نمی‌کنند.
            </Typography>
            <Stack spacing={2} sx={{ mt: 1 }}>
              <TextField type="date" label="مهلت تا" InputLabelProps={{ shrink: true }}
                value={deferRow.deferred_until || ""} onChange={(e) => setDeferRow({ ...deferRow, deferred_until: e.target.value })} />
              <TextField label="یادداشت (اختیاری)" value={deferRow.defer_note || ""}
                onChange={(e) => setDeferRow({ ...deferRow, defer_note: e.target.value })} />
            </Stack>
          </DialogContent>
          <DialogActions>
            <Button color="error" onClick={() => { setDeferRow({ ...deferRow, deferred_until: "" }); setTimeout(() => saveDefer.mutate(), 0); }}>حذف مهلت</Button>
            <Button onClick={() => setDeferRow(null)}>انصراف</Button>
            <Button variant="contained" onClick={() => saveDefer.mutate()} disabled={saveDefer.isPending || !deferRow.deferred_until}>ثبت مهلت</Button>
          </DialogActions>
        </>)}
      </Dialog>

      {/* Detail dialog */}
      <Dialog open={!!detail} onClose={() => setDetail(null)} fullWidth maxWidth="md">
        {detail && (<>
          <DialogTitle>فاکتور {detail.reseller_name} — دوره {detail.period_label}</DialogTitle>
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
              <Table size="small" stickyHeader>
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
            <Button onClick={() => setDetail(null)}>بستن</Button>
          </DialogActions>
        </>)}
      </Dialog>
      {node}
    </Box>
  );
}
