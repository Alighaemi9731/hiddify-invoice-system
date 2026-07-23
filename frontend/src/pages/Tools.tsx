import { useState } from "react";
import {
  Alert, Box, Button, Card, CardContent, Checkbox, Chip, CircularProgress, Dialog, DialogActions,
  DialogContent, DialogTitle, Divider, IconButton, InputAdornment, Stack, Table, TableBody,
  TableCell, TableHead, TableRow, TextField, Tooltip, Typography,
} from "@mui/material";
import SearchIcon from "@mui/icons-material/esm/Search";
import DeleteOutlineIcon from "@mui/icons-material/esm/DeleteOutline";
import DeleteForeverIcon from "@mui/icons-material/esm/DeleteForever";
import LinkOffIcon from "@mui/icons-material/esm/LinkOff";
import PersonRemoveIcon from "@mui/icons-material/esm/PersonRemove";
import RestoreIcon from "@mui/icons-material/esm/Restore";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  searchEndUsers, removeEndUser, ToolsEndUser,
  listResellers, unbindResellerTelegram, ResellerRow,
  previewAdminDelete, deleteAdminCascade,
  listPanels, recoveryCandidates, recoveryRestore, RecoveryPanel, RecoveryResult,
} from "../api/client";
import { errMsg, useToast } from "../components/Toast";
import { DataState } from "../components/DataState";
import { fmtGb, fmtNum, fmtDate, fmtDateTime } from "../format";
import { useXsFullScreen } from "../responsive";

// ── Section 1: remove a mistaken end-user from billing ────────────────────────
function RemoveUserTool() {
  const xsFull = useXsFullScreen();
  const qc = useQueryClient();
  const { node: toast, show } = useToast();
  const [input, setInput] = useState("");
  const [term, setTerm] = useState("");
  const [delRow, setDelRow] = useState<ToolsEndUser | null>(null);

  const { data = [], isFetching, isError, refetch } = useQuery({
    queryKey: ["tools-endusers", term],
    queryFn: () => searchEndUsers(term),
    enabled: term.length > 0,
  });
  const del = useMutation({
    mutationFn: (id: number) => removeEndUser(id),
    onSuccess: (r: any) => {
      show(`کاربر «${r?.name || ""}» از محاسبه حذف شد.`);
      setDelRow(null);
      qc.invalidateQueries({ queryKey: ["tools-endusers"] });
    },
    onError: (e) => show(errMsg(e), "error"),
  });

  const submit = () => setTerm(input.trim());

  return (
    <Card>
      {toast}
      <CardContent>
        <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 1 }}>
          <PersonRemoveIcon color="error" />
          <Typography variant="h6">حذفِ کاربر از محاسبهٔ فاکتور</Typography>
        </Stack>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
          کاربری که به‌اشتباه ساخته شده و مبلغِ فاکتور را افزایش می‌دهد، با نام یا UUID پیدا و حذف کنید.
          فقط دادهٔ این کاربر از سامانه پاک می‌شود (پنل، فاکتورهای ارسال‌شده و تاریخچهٔ مالی تغییر نمی‌کنند).
        </Typography>
        <Stack direction="row" spacing={1} sx={{ mb: 2 }}>
          <TextField
            size="small" fullWidth placeholder="جستجوی نام یا UUID کاربر…"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") submit(); }}
            InputProps={{ startAdornment: (
              <InputAdornment position="start"><SearchIcon fontSize="small" /></InputAdornment>
            ) }}
          />
          <Button variant="contained" onClick={submit} disabled={!input.trim()}>جستجو</Button>
        </Stack>

        {term && (
          <DataState isLoading={isFetching} isError={isError} rows={4} onRetry={refetch}>
          <Box sx={{ overflowX: "auto" }}>
            <Table size="small" className="resp-table">
              <TableHead>
                <TableRow>
                  <TableCell>نام</TableCell>
                  <TableCell>UUID</TableCell>
                  <TableCell>پنل</TableCell>
                  <TableCell>نماینده</TableCell>
                  <TableCell align="left">سهمیه</TableCell>
                  <TableCell align="left">مصرف</TableCell>
                  <TableCell>شروع</TableCell>
                  <TableCell>روی پنل؟</TableCell>
                  <TableCell align="center">حذف</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {data.map((u) => (
                  <TableRow key={u.id} hover>
                    <TableCell>{u.name || "—"}</TableCell>
                    <TableCell dir="ltr" sx={{ fontFamily: "monospace", fontSize: 12 }}>…{u.user_uuid.slice(-8)}</TableCell>
                    <TableCell>{u.panel_key}</TableCell>
                    <TableCell>{u.reseller_name || "—"}</TableCell>
                    <TableCell align="left">{fmtGb(u.usage_limit_gb)}</TableCell>
                    <TableCell align="left">{fmtGb(u.current_usage_gb)}</TableCell>
                    <TableCell dir="ltr">{fmtDate(u.start_date)}</TableCell>
                    <TableCell>
                      <Chip size="small" color={u.present_on_panel ? "warning" : "default"}
                        variant={u.present_on_panel ? "filled" : "outlined"}
                        label={u.present_on_panel ? "بله" : "خیر"} />
                    </TableCell>
                    <TableCell align="center">
                      <Tooltip title="حذف از محاسبه">
                        <IconButton size="small" color="error" onClick={() => setDelRow(u)}>
                          <DeleteOutlineIcon fontSize="small" />
                        </IconButton>
                      </Tooltip>
                    </TableCell>
                  </TableRow>
                ))}
                {!isFetching && data.length === 0 && (
                  <TableRow><TableCell colSpan={9} align="center" sx={{ color: "text.secondary", py: 3 }}>
                    کاربری یافت نشد.
                  </TableCell></TableRow>
                )}
              </TableBody>
            </Table>
          </Box>
          </DataState>
        )}
      </CardContent>

      <Dialog open={!!delRow} onClose={() => setDelRow(null)} fullWidth maxWidth="xs" fullScreen={xsFull}>
        {delRow && (
          <>
            <DialogTitle>حذفِ کاربر — {delRow.name || delRow.user_uuid.slice(-8)}</DialogTitle>
            <DialogContent>
              <Typography variant="body2" sx={{ mb: 1 }}>
                دادهٔ این کاربر (سهمیهٔ <b>{fmtGb(delRow.usage_limit_gb)}</b> روی پنلِ <b>{delRow.panel_key}</b>،
                نمایندهٔ <b>{delRow.reseller_name || "—"}</b>) از سامانه حذف می‌شود تا در فاکتور محاسبه نشود
                (هم سهمیه و هم مصرف/متره).
              </Typography>
              {delRow.present_on_panel && (
                <Typography variant="body2" color="error" sx={{ fontWeight: 700, mb: 1 }}>
                  ⚠️ این کاربر هنوز روی پنل موجود است؛ اگر اول از پنل حذفش نکنید، در همگام‌سازیِ بعدی دوباره اضافه می‌شود.
                </Typography>
              )}
              <Typography variant="body2" color="text.secondary">
                برای اعمال روی فاکتورِ این دوره، پیش‌نویس را دوباره «صدور» یا فاکتورِ ارسال‌شده را «بازمحاسبه» کنید.
              </Typography>
            </DialogContent>
            <DialogActions>
              <Button onClick={() => setDelRow(null)}>انصراف</Button>
              <Button variant="contained" color="error" disabled={del.isPending}
                onClick={() => del.mutate(delRow.id)}>حذف</Button>
            </DialogActions>
          </>
        )}
      </Dialog>
    </Card>
  );
}

// ── Section 2: disconnect a reseller's Telegram binding ───────────────────────
function UnbindTelegramTool() {
  const xsFull = useXsFullScreen();
  const qc = useQueryClient();
  const { node: toast, show } = useToast();
  const [input, setInput] = useState("");
  const [term, setTerm] = useState("");
  const [unbindRow, setUnbindRow] = useState<ResellerRow | null>(null);

  const { data = [], isFetching, isError, refetch } = useQuery({
    queryKey: ["tools-resellers", term],
    queryFn: () => listResellers({ q: term, registered: true }),
    enabled: term.length > 0,
  });
  const unbind = useMutation({
    mutationFn: (id: number) => unbindResellerTelegram(id),
    onSuccess: () => {
      show("اتصالِ تلگرام قطع شد؛ نماینده می‌تواند با حسابِ جدید دوباره ثبت کند.");
      setUnbindRow(null);
      qc.invalidateQueries({ queryKey: ["tools-resellers"] });
    },
    onError: (e) => show(errMsg(e), "error"),
  });

  const submit = () => setTerm(input.trim());

  return (
    <Card>
      {toast}
      <CardContent>
        <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 1 }}>
          <LinkOffIcon color="warning" />
          <Typography variant="h6">قطع اتصالِ تلگرامِ نماینده</Typography>
        </Stack>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
          اگر نماینده‌ای به حسابِ تلگرامی که با آن ثبت شده دسترسی ندارد، اتصال را آزاد کنید تا با حسابِ
          جدید دوباره پنلش را ثبت کند. فقط نماینده‌هایِ متصل نمایش داده می‌شوند.
        </Typography>
        <Stack direction="row" spacing={1} sx={{ mb: 2 }}>
          <TextField
            size="small" fullWidth placeholder="جستجوی نام یا شناسهٔ نماینده…"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") submit(); }}
            InputProps={{ startAdornment: (
              <InputAdornment position="start"><SearchIcon fontSize="small" /></InputAdornment>
            ) }}
          />
          <Button variant="contained" onClick={submit} disabled={!input.trim()}>جستجو</Button>
        </Stack>

        {term && (
          <DataState isLoading={isFetching} isError={isError} rows={4} onRetry={refetch}>
          <Box sx={{ overflowX: "auto" }}>
            <Table size="small" className="resp-table">
              <TableHead>
                <TableRow>
                  <TableCell>نام</TableCell>
                  <TableCell>پنل</TableCell>
                  <TableCell>حساب تلگرام</TableCell>
                  <TableCell align="center">قطع اتصال</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {data.map((r) => (
                  <TableRow key={r.id} hover>
                    <TableCell>{r.name}</TableCell>
                    <TableCell>{r.panel_key}</TableCell>
                    <TableCell dir="ltr">{r.username ? `@${r.username}` : r.bot_chat_id || "—"}</TableCell>
                    <TableCell align="center">
                      <Tooltip title="قطع اتصالِ تلگرام">
                        <IconButton size="small" color="warning" onClick={() => setUnbindRow(r)}>
                          <LinkOffIcon fontSize="small" />
                        </IconButton>
                      </Tooltip>
                    </TableCell>
                  </TableRow>
                ))}
                {!isFetching && data.length === 0 && (
                  <TableRow><TableCell colSpan={4} align="center" sx={{ color: "text.secondary", py: 3 }}>
                    نمایندهٔ متصلی یافت نشد.
                  </TableCell></TableRow>
                )}
              </TableBody>
            </Table>
          </Box>
          </DataState>
        )}
      </CardContent>

      <Dialog open={!!unbindRow} onClose={() => setUnbindRow(null)} fullWidth maxWidth="xs" fullScreen={xsFull}>
        {unbindRow && (
          <>
            <DialogTitle>قطع اتصالِ تلگرام — {unbindRow.name}</DialogTitle>
            <DialogContent>
              <Typography variant="body2" sx={{ mb: 1 }}>
                اتصالِ این نماینده به حسابِ تلگرامِ فعلی
                {unbindRow.username
                  ? <> (<b dir="ltr">@{unbindRow.username}</b>)</>
                  : unbindRow.bot_chat_id
                    ? <> (<b dir="ltr">{unbindRow.bot_chat_id}</b>)</>
                    : null}
                {" "}آزاد می‌شود تا بتواند با یک حسابِ تلگرامِ <b>جدید</b> دوباره پنلش را ثبت کند.
              </Typography>
              <Typography variant="body2" color="text.secondary">
                این کار فقط اتصالِ تلگرام را پاک می‌کند؛ هیچ تغییری روی پنلِ هیدیفای، کاربران یا فاکتورها
                ایجاد نمی‌شود. برای ثبتِ دوباره، نماینده لینکِ پنلش را در ربات می‌فرستد.
              </Typography>
            </DialogContent>
            <DialogActions>
              <Button onClick={() => setUnbindRow(null)}>انصراف</Button>
              <Button variant="contained" color="warning" disabled={unbind.isPending}
                onClick={() => unbind.mutate(unbindRow.id)}>قطعِ اتصال</Button>
            </DialogActions>
          </>
        )}
      </Dialog>
    </Card>
  );
}

// ── Section 3: cascade-delete an admin (reseller) from the Hiddify panel ──────
function DeleteAdminTool() {
  const xsFull = useXsFullScreen();
  const qc = useQueryClient();
  const { node: toast, show } = useToast();
  const [input, setInput] = useState("");
  const [term, setTerm] = useState("");
  const [delRow, setDelRow] = useState<ResellerRow | null>(null);

  const { data = [], isFetching, isError, refetch } = useQuery({
    queryKey: ["tools-del-resellers", term],
    queryFn: () => listResellers({ q: term }),
    enabled: term.length > 0,
  });
  // Subtree footprint of the selected reseller (sub-resellers + users), for the confirm dialog.
  const { data: scope, isLoading: scopeLoading } = useQuery({
    queryKey: ["tools-del-preview", delRow?.id],
    queryFn: () => previewAdminDelete(delRow!.id),
    enabled: !!delRow,
  });
  const del = useMutation({
    mutationFn: (id: number) => deleteAdminCascade(id),
    onSuccess: (r: any) => {
      show(`حذفِ «${r?.name || ""}» در صف قرار گرفت — در پس‌زمینه و مرحله‌ای انجام می‌شود.`, "info");
      setDelRow(null);
      qc.invalidateQueries({ queryKey: ["tools-del-resellers"] });
    },
    onError: (e) => show(errMsg(e), "error"),
  });

  const submit = () => setTerm(input.trim());

  return (
    <Card>
      {toast}
      <CardContent>
        <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 1 }}>
          <DeleteForeverIcon color="error" />
          <Typography variant="h6">حذفِ ادمین از پنلِ هیدیفای</Typography>
        </Stack>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
          یک ادمین به‌همراهِ <b>همهٔ زیرمجموعه‌ها و کاربرانِ آن‌ها</b> را به‌صورتِ خودکار از پنل و سامانه
          حذف می‌کند (به‌جای حذفِ دستیِ تک‌تک). فرایند در پس‌زمینه و مرحله‌ای انجام می‌شود تا فشارِ زیادی به پنل
          وارد نشود؛ تاریخچهٔ مالی حفظ می‌شود.
        </Typography>
        <Stack direction="row" spacing={1} sx={{ mb: 2 }}>
          <TextField
            size="small" fullWidth placeholder="جستجوی نام یا شناسهٔ نماینده…"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") submit(); }}
            InputProps={{ startAdornment: (
              <InputAdornment position="start"><SearchIcon fontSize="small" /></InputAdornment>
            ) }}
          />
          <Button variant="contained" onClick={submit} disabled={!input.trim()}>جستجو</Button>
        </Stack>

        {term && (
          <DataState isLoading={isFetching} isError={isError} rows={4} onRetry={refetch}>
          <Box sx={{ overflowX: "auto" }}>
            <Table size="small" className="resp-table">
              <TableHead>
                <TableRow>
                  <TableCell>نام</TableCell>
                  <TableCell>پنل</TableCell>
                  <TableCell align="left">کاربران</TableCell>
                  <TableCell align="center">حذفِ کامل</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {data.map((r) => (
                  <TableRow key={r.id} hover>
                    <TableCell>{r.name}{r.is_owner && <Chip size="small" label="مالک" sx={{ ml: 0.5 }} />}</TableCell>
                    <TableCell>{r.panel_key}</TableCell>
                    <TableCell align="left">{fmtNum(r.users_count)}</TableCell>
                    <TableCell align="center">
                      <Tooltip title={r.is_owner ? "مالکِ پنل قابلِ حذف نیست" : "حذفِ کامل از پنل"}>
                        <span>
                          <IconButton size="small" color="error" disabled={r.is_owner}
                            onClick={() => setDelRow(r)}>
                            <DeleteForeverIcon fontSize="small" />
                          </IconButton>
                        </span>
                      </Tooltip>
                    </TableCell>
                  </TableRow>
                ))}
                {!isFetching && data.length === 0 && (
                  <TableRow><TableCell colSpan={4} align="center" sx={{ color: "text.secondary", py: 3 }}>
                    نماینده‌ای یافت نشد.
                  </TableCell></TableRow>
                )}
              </TableBody>
            </Table>
          </Box>
          </DataState>
        )}
      </CardContent>

      <Dialog open={!!delRow} onClose={() => setDelRow(null)} fullWidth maxWidth="xs" fullScreen={xsFull}>
        {delRow && (
          <>
            <DialogTitle>حذفِ کاملِ ادمین — {delRow.name}</DialogTitle>
            <DialogContent>
              <Typography variant="body2" color="error" sx={{ fontWeight: 700, mb: 1 }}>
                ⚠️ این کار برگشت‌ناپذیر است.
              </Typography>
              <Typography variant="body2" sx={{ mb: 1 }}>
                این ادمین، <b>{scopeLoading ? "…" : fmtNum(scope?.sub_reseller_count ?? 0)}</b> زیرمجموعه و
                <b> {scopeLoading ? "…" : fmtNum(scope?.user_count ?? 0)}</b> کاربر روی پنلِ
                <b> {delRow.panel_key}</b> — همگی از <b>پنلِ هیدیفای</b> و از <b>سامانه</b> حذف می‌شوند.
                فرایند در پس‌زمینه و مرحله‌ای انجام می‌شود (ممکن است برای ادمینِ بزرگ کمی طول بکشد).
              </Typography>
              <Typography variant="body2" color="text.secondary">
                تاریخچهٔ مالی (لجر) حفظ می‌شود. اتصالِ تلگرام و دسترسیِ این ادمین هم از بین می‌رود.
              </Typography>
            </DialogContent>
            <DialogActions>
              <Button onClick={() => setDelRow(null)}>انصراف</Button>
              <Button variant="contained" color="error"
                disabled={del.isPending || scopeLoading || !!scope?.is_owner}
                onClick={() => del.mutate(delRow.id)}>حذفِ کامل</Button>
            </DialogActions>
          </>
        )}
      </Dialog>
    </Card>
  );
}

// ── Section 4: recover users a panel backup-rollback removed ──────────────────
function RecoverUsersTool() {
  const xsFull = useXsFullScreen();
  const { node: toast, show } = useToast();
  const [sel, setSel] = useState<Set<number>>(new Set());
  const [lookback, setLookback] = useState(2);
  const [result, setResult] = useState<RecoveryPanel[] | null>(null);
  const [chosen, setChosen] = useState<Set<string>>(new Set());
  const [confirm, setConfirm] = useState(false);
  const [report, setReport] = useState<RecoveryResult | null>(null);

  const { data: panels = [] } = useQuery<any[]>({ queryKey: ["panels"], queryFn: listPanels });
  const keyOf = (u: { panel_id: number; user_uuid: string }) => `${u.panel_id}|${u.user_uuid}`;

  const detect = useMutation({
    mutationFn: () => recoveryCandidates([...sel], lookback),
    onSuccess: (r) => {
      setResult(r);
      // Default-select NOTHING — the owner reviews and picks the cluster that is THEIR rollback (the
      // migration loss is indistinguishable from ordinary churn by size alone, so we never guess).
      setChosen(new Set());
      setReport(null);
    },
    onError: (e) => show(errMsg(e), "error"),
  });

  const restore = useMutation({
    mutationFn: () => {
      const users = [...chosen].map((k) => { const [pid, uuid] = k.split("|"); return { panel_id: Number(pid), user_uuid: uuid }; });
      return recoveryRestore(users, false);
    },
    onSuccess: (r) => {
      setReport(r);
      setConfirm(false);
      const done = new Set<string>();
      [...r.created, ...r.skipped].forEach((u: any) => done.add(`${u.panel_id}|${u.user_uuid}`));
      setResult((prev) => !prev ? prev : prev
        .map((p) => {
          const clusters = p.clusters
            .map((c) => { const users = c.users.filter((u) => !done.has(keyOf(u))); return { ...c, users, count: users.length }; })
            .filter((c) => c.users.length > 0);
          return { ...p, clusters, total_lost: clusters.reduce((a, c) => a + c.count, 0) };
        })
        .filter((p) => p.total_lost > 0));
      setChosen((s) => { const n = new Set(s); done.forEach((k) => n.delete(k)); return n; });
      show(`${r.counts.created} کاربر بازیابی شد.`);
    },
    onError: (e) => { setConfirm(false); show(errMsg(e), "error"); },
  });

  const togglePanel = (id: number) => setSel((s) => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n; });
  const toggleUser = (k: string) => setChosen((s) => { const n = new Set(s); n.has(k) ? n.delete(k) : n.add(k); return n; });
  const totalLost = (result || []).reduce((a, p) => a + p.total_lost, 0);

  return (
    <Card>
      <CardContent>
        {toast}
        <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 0.5 }}>
          <RestoreIcon color="primary" />
          <Typography variant="h6">بازیابی کاربران</Typography>
        </Stack>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
          کاربرانی که به‌دلیلِ بازگرداندنِ پنل به یک پشتیبانِ قدیمی‌تر (مهاجرت/بازیابی) از پنل حذف شده‌اند
          ولی هنوز در حافظهٔ سامانه‌اند — با همان UUID و زیرِ همان ادمینِ اصلی دوباره ساخته می‌شوند.
        </Typography>

        <Typography variant="body2" sx={{ mb: 0.5 }}>۱) پنل‌های موردِ نظر را انتخاب کنید:</Typography>
        <Stack direction="row" flexWrap="wrap" gap={1} sx={{ mb: 1.5 }}>
          {panels.map((p) => (
            <Chip key={p.id} label={p.host || p.key || `#${p.id}`} onClick={() => togglePanel(p.id)}
              color={sel.has(p.id) ? "primary" : "default"} variant={sel.has(p.id) ? "filled" : "outlined"} />
          ))}
        </Stack>
        <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 2 }}>
          <TextField label="بازه (روزِ اخیر)" type="number" size="small" value={lookback}
            onChange={(e) => setLookback(Math.max(1, Math.min(90, Number(e.target.value) || 7)))}
            sx={{ width: 150 }} inputProps={{ dir: "ltr", min: 1, max: 90 }} />
          <Button variant="contained" onClick={() => detect.mutate()} disabled={sel.size === 0 || detect.isPending}
            startIcon={detect.isPending ? <CircularProgress size={16} /> : <SearchIcon />}>
            ۲) بررسیِ کاربرانِ گم‌شده
          </Button>
        </Stack>

        {detect.isPending && <DataState isLoading rows={3}><Box /></DataState>}

        {result && totalLost === 0 && (
          <Alert severity="success">هیچ کاربرِ گم‌شده‌ای در بازهٔ انتخابی پیدا نشد. ✅</Alert>
        )}

        {result && totalLost > 0 && (
          <Alert severity="info" sx={{ mb: 1.5 }}>
            کاربرانِ گم‌شده بر اساسِ «لحظهٔ ناپدید شدن» خوشه‌بندی و از بزرگ به کوچک مرتب شده‌اند.
            <b> خوشهٔ مربوط به زمانِ بازیابی/مهاجرتِ خود را انتخاب کنید</b> — نشانه‌ها: تعدادِ زیاد، هم‌زمانی
            با زمانی که پنل را جابه‌جا کرده‌اید، و برچسبِ «قطعیِ پنل». خوشه‌های کوچکِ پراکنده معمولاً حذف‌های
            عادیِ روزانه‌اند؛ آن‌ها را انتخاب نکنید. (هیچ‌چیز به‌صورت پیش‌فرض انتخاب نشده تا به‌اشتباه چیزی اضافه نشود.)
          </Alert>
        )}

        {result && totalLost > 0 && result.map((p) => (
          <Box key={p.panel_id} sx={{ mb: 2 }}>
            <Divider sx={{ my: 1 }}><Chip size="small" color="primary" variant="outlined"
              label={`${p.panel_key} — ${fmtNum(p.total_lost)} کاربرِ گم‌شده`} /></Divider>
            {p.clusters.map((c) => (
              <Box key={c.key} sx={{ mb: 1.5 }}>
                <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 0.5, flexWrap: "wrap" }}>
                  <Chip size="small" color={c.count >= 8 ? "warning" : "default"}
                    label={`${fmtNum(c.count)} کاربر · آخرین حضور: ${fmtDateTime(c.last_seen_at)}`} />
                  {c.had_failure && <Chip size="small" color="error" variant="outlined" label="⚠️ قطعیِ پنل در این زمان" />}
                  {c.drop_size > 0 && <Typography variant="caption" color="text.secondary">افتِ شمارشِ پنل: {fmtNum(c.drop_size)}</Typography>}
                </Stack>
                <Box sx={{ overflowX: "auto" }}>
                <Table size="small" className="resp-table">
                  <TableHead><TableRow>
                    <TableCell padding="checkbox">
                      <Checkbox size="small"
                        checked={c.users.some((u) => u.has_admin) && c.users.every((u) => !u.has_admin || chosen.has(keyOf(u)))}
                        onChange={(e) => setChosen((s) => { const n = new Set(s); c.users.forEach((u) => { if (u.has_admin) { e.target.checked ? n.add(keyOf(u)) : n.delete(keyOf(u)); } }); return n; })} />
                    </TableCell>
                    <TableCell>نام</TableCell><TableCell>ادمین</TableCell><TableCell>حجم</TableCell>
                    <TableCell>مدت</TableCell><TableCell>تاریخِ شروع (میلادی)</TableCell>
                    <TableCell>افزوده‌شده</TableCell><TableCell>مصرف</TableCell>
                    <TableCell>UUID</TableCell><TableCell>وضعیت</TableCell>
                  </TableRow></TableHead>
                  <TableBody>
                    {c.users.map((u) => (
                      <TableRow key={u.user_uuid} hover>
                        <TableCell padding="checkbox">
                          <Tooltip title={u.has_admin ? "" : "ادمینِ این کاربر پیدا نشد — قابلِ بازیابی نیست"}>
                            <span><Checkbox size="small" disabled={!u.has_admin}
                              checked={chosen.has(keyOf(u))} onChange={() => toggleUser(keyOf(u))} /></span>
                          </Tooltip>
                        </TableCell>
                        <TableCell>{u.name || "—"}</TableCell>
                        <TableCell>{u.admin_name || <Chip size="small" color="error" label="؟ ادمین" />}</TableCell>
                        <TableCell>{fmtGb(u.gb)}</TableCell>
                        <TableCell>{fmtNum(u.days)} روز</TableCell>
                        <TableCell dir="ltr">{u.start_date || "—"}</TableCell>
                        <TableCell dir="ltr">{fmtDateTime(u.created_at)}</TableCell>
                        <TableCell>{fmtGb(u.current_usage_gb)}</TableCell>
                        <TableCell><Box component="code" sx={{ fontSize: 11 }} dir="ltr">{u.user_uuid.slice(0, 13)}</Box></TableCell>
                        <TableCell>{u.enable ? "فعال" : "غیرفعال"}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
                </Box>
              </Box>
            ))}
          </Box>
        ))}

        {result && totalLost > 0 && (
          <Stack direction="row" alignItems="center" spacing={2} sx={{ mt: 1 }}>
            <Typography variant="body2">{fmtNum(chosen.size)} کاربر انتخاب شده</Typography>
            <Button variant="contained" startIcon={<RestoreIcon />} disabled={chosen.size === 0}
              onClick={() => setConfirm(true)}>۳) بازیابیِ انتخاب‌شده‌ها</Button>
          </Stack>
        )}

        {report && (
          <Alert severity={report.counts.errors ? "warning" : "success"} sx={{ mt: 2 }}>
            <b>{fmtNum(report.counts.created)}</b> ساخته شد، {fmtNum(report.counts.skipped)} از قبل موجود بود
            {report.counts.errors ? <>، <b>{fmtNum(report.counts.errors)}</b> خطا</> : null}.
            {report.errors.length > 0 && (
              <Box sx={{ mt: 1 }}>{report.errors.map((e: any, i: number) => (
                <Typography key={i} variant="caption" display="block">• {e.name || e.user_uuid?.slice(0, 8)}: {e.reason}</Typography>
              ))}</Box>
            )}
          </Alert>
        )}

        <Dialog open={confirm} onClose={() => setConfirm(false)} fullWidth maxWidth="xs" fullScreen={xsFull}>
          <DialogTitle>تأییدِ بازیابی</DialogTitle>
          <DialogContent>
            <Typography>
              {fmtNum(chosen.size)} کاربر با همان UUID و زیرِ همان ادمینِ اصلی روی پنل ساخته می‌شوند.
              کاربری که هم‌اکنون روی پنل باشد رد می‌شود (تکراری ساخته نمی‌شود).
            </Typography>
          </DialogContent>
          <DialogActions>
            <Button onClick={() => setConfirm(false)}>انصراف</Button>
            <Button variant="contained" onClick={() => restore.mutate()} disabled={restore.isPending}
              startIcon={restore.isPending ? <CircularProgress size={16} /> : <RestoreIcon />}>
              بله، بازیابی شود
            </Button>
          </DialogActions>
        </Dialog>
      </CardContent>
    </Card>
  );
}

export default function Tools() {
  return (
    <Box>
      <Typography variant="h5" sx={{ mb: 0.4 }}>ابزارها</Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 2.5 }}>
        عملیاتِ نادر و ویژه (با احتیاط استفاده شود)
      </Typography>
      <Stack spacing={2} sx={{ maxWidth: 1000 }}>
        <RecoverUsersTool />
        <RemoveUserTool />
        <UnbindTelegramTool />
        <DeleteAdminTool />
      </Stack>
    </Box>
  );
}
