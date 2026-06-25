import { useState } from "react";
import {
  Box, Button, Card, CardContent, Chip, Dialog, DialogActions, DialogContent, DialogTitle,
  IconButton, InputAdornment, Stack, Table, TableBody, TableCell, TableHead, TableRow,
  TextField, Tooltip, Typography,
} from "@mui/material";
import SearchIcon from "@mui/icons-material/esm/Search";
import DeleteOutlineIcon from "@mui/icons-material/esm/DeleteOutline";
import DeleteForeverIcon from "@mui/icons-material/esm/DeleteForever";
import LinkOffIcon from "@mui/icons-material/esm/LinkOff";
import PersonRemoveIcon from "@mui/icons-material/esm/PersonRemove";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  searchEndUsers, removeEndUser, ToolsEndUser,
  listResellers, unbindResellerTelegram, ResellerRow,
  previewAdminDelete, deleteAdminCascade,
} from "../api/client";
import { errMsg, useToast } from "../components/Toast";
import { fmtGb, fmtNum, fmtDate } from "../format";

// ── Section 1: remove a mistaken end-user from billing ────────────────────────
function RemoveUserTool() {
  const qc = useQueryClient();
  const { node: toast, show } = useToast();
  const [input, setInput] = useState("");
  const [term, setTerm] = useState("");
  const [delRow, setDelRow] = useState<ToolsEndUser | null>(null);

  const { data = [], isFetching } = useQuery({
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
          کاربری که اشتباه ساخته شده و فاکتور را بزرگ می‌کند، با نام یا UUID پیدا کنید و حذف کنید.
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
          <Box sx={{ overflowX: "auto" }}>
            <Table size="small">
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
        )}
      </CardContent>

      <Dialog open={!!delRow} onClose={() => setDelRow(null)} fullWidth maxWidth="xs">
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
  const qc = useQueryClient();
  const { node: toast, show } = useToast();
  const [input, setInput] = useState("");
  const [term, setTerm] = useState("");
  const [unbindRow, setUnbindRow] = useState<ResellerRow | null>(null);

  const { data = [], isFetching } = useQuery({
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
          <Box sx={{ overflowX: "auto" }}>
            <Table size="small">
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
        )}
      </CardContent>

      <Dialog open={!!unbindRow} onClose={() => setUnbindRow(null)} fullWidth maxWidth="xs">
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
  const qc = useQueryClient();
  const { node: toast, show } = useToast();
  const [input, setInput] = useState("");
  const [term, setTerm] = useState("");
  const [delRow, setDelRow] = useState<ResellerRow | null>(null);

  const { data = [], isFetching } = useQuery({
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
          حذف می‌کند (به‌جای حذفِ دستیِ تک‌تک). فرایند در پس‌زمینه، مرحله‌ای و با‌احتیاطِ فشار روی پنل
          انجام می‌شود؛ تاریخچهٔ مالی حفظ می‌شود.
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
          <Box sx={{ overflowX: "auto" }}>
            <Table size="small">
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
        )}
      </CardContent>

      <Dialog open={!!delRow} onClose={() => setDelRow(null)} fullWidth maxWidth="xs">
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

export default function Tools() {
  return (
    <Box>
      <Typography variant="h5" sx={{ mb: 0.4 }}>ابزارها</Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 2.5 }}>
        عملیاتِ نادر و ویژه (با احتیاط استفاده شود)
      </Typography>
      <Stack spacing={2} sx={{ maxWidth: 1000 }}>
        <RemoveUserTool />
        <UnbindTelegramTool />
        <DeleteAdminTool />
      </Stack>
    </Box>
  );
}
