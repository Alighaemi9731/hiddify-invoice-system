import { useState } from "react";
import {
  Box, Button, Card, Chip, Dialog, DialogActions, DialogContent, DialogTitle,
  IconButton, Stack, Table, TableBody, TableCell, TableHead, TableRow, TextField,
  Tooltip, Switch, FormControlLabel, Typography,
} from "@mui/material";
import SyncIcon from "@mui/icons-material/Sync";
import EditIcon from "@mui/icons-material/Edit";
import DeleteIcon from "@mui/icons-material/Delete";
import WifiTetheringIcon from "@mui/icons-material/WifiTethering";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  listPanels, createPanel, updatePanel, deletePanel, syncPanel, testPanel,
} from "../api/client";
import { useToast, errMsg } from "../components/Toast";
import { useSort, SortTh } from "../components/sortable";
import { fmtNum, fmtDate } from "../format";

const EMPTY = { key: "", name: "", host: "", proxy_path: "", owner_uuid: "", admin_api_key: "", enabled: true };

const UUID_RE = /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/;

// Parse a pasted admin link like
//   https://<host>/<proxy_path>/<owner_uuid>/admin/adminuser/
// into the panel fields. Everything after the UUID is ignored.
function parsePanelLink(raw: string): null | { host: string; proxy_path: string; owner_uuid: string; key: string } {
  const text = (raw || "").trim();
  if (!text) return null;
  let url: URL;
  try {
    url = new URL(text.includes("://") ? text : `https://${text}`);
  } catch {
    return null;
  }
  const segs = url.pathname.split("/").filter(Boolean);
  const idx = segs.findIndex((s) => UUID_RE.test(s));
  if (idx === -1) return null;
  return {
    host: url.hostname,
    proxy_path: idx > 0 ? segs[idx - 1] : "",
    owner_uuid: segs[idx].toLowerCase(),
    key: (url.hostname.split(".")[0] || "").toLowerCase(),
  };
}

export default function Panels() {
  const qc = useQueryClient();
  const { node, show } = useToast();
  const { data = [], isLoading } = useQuery({
    queryKey: ["panels"], queryFn: listPanels,
    // While any panel is still syncing (status "unknown"), poll so its chip flips to
    // موفق/خطا on its own once the background sync finishes — no manual refresh needed.
    refetchInterval: (q: any) =>
      (q.state.data || []).some((p: any) => p.status === "unknown") ? 3000 : false,
  });
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState<any>(EMPTY);
  const [link, setLink] = useState("");
  const refresh = () => qc.invalidateQueries({ queryKey: ["panels"] });

  const save = useMutation({
    mutationFn: () => {
      // On edit, don't overwrite the stored secret fields with blanks.
      const payload: any = { ...form };
      if (form.id) {
        if (!payload.proxy_path) delete payload.proxy_path;
        if (!payload.admin_api_key) delete payload.admin_api_key;
      }
      return form.id ? updatePanel(form.id, payload) : createPanel(payload);
    },
    onSuccess: () => { show("ذخیره شد"); setOpen(false); refresh(); },
    onError: (e) => show(errMsg(e), "error"),
  });

  const applyLink = (value: string) => {
    setLink(value);
    const p = parsePanelLink(value);
    if (p) setForm((f: any) => ({
      ...f, host: p.host, proxy_path: p.proxy_path, owner_uuid: p.owner_uuid,
      key: f.key || p.key, name: f.name || p.key,
    }));
  };
  const doSync = useMutation({
    mutationFn: (id: number) => syncPanel(id),
    onSuccess: () => {
      show("همگام‌سازی آغاز شد؛ نتیجه تا چند لحظه دیگر به‌روزرسانی می‌شود.", "info");
      refresh();
      // Poll a few times so the status flips to «موفق» without a manual reload.
      [4000, 9000, 16000, 25000].forEach((ms) => setTimeout(refresh, ms));
    },
    onError: (e) => show(errMsg(e), "error"),
  });
  const doTest = useMutation({
    mutationFn: (id: number) => testPanel(id),
    onSuccess: (r) => show(r.ok ? `اتصال موفق — ${r.admin_count} ادمین / ${r.user_count} کاربر` : `ناموفق: ${r.error}`, r.ok ? "success" : "error"),
    onError: (e) => show(errMsg(e), "error"),
  });
  const doDelete = useMutation({
    mutationFn: (id: number) => deletePanel(id),
    onSuccess: () => { show("حذف شد"); refresh(); },
    onError: (e) => show(errMsg(e), "error"),
  });

  const { sorted, key, dir, toggle } = useSort(data, "key", "asc");
  const edit = (p?: any) => { setForm(p ? { ...p, proxy_path: "", admin_api_key: "" } : EMPTY); setLink(""); setOpen(true); };

  const statusChip = (s: string) => {
    const map: any = { ok: ["موفق", "success"], error: ["خطا", "error"], unknown: ["در حال همگام‌سازی…", "info"], disabled: ["غیرفعال", "warning"] };
    const [lbl, color] = map[s] || [s, "default"];
    return <Chip size="small" label={lbl} color={color} />;
  };

  return (
    <Box>
      <Stack direction="row" justifyContent="space-between" sx={{ mb: 2 }}>
        <Typography variant="body2" color="text.secondary">
          پنل‌های هیدیفای متصل (حداکثر ۱۰)
        </Typography>
        <Button variant="contained" onClick={() => edit()}>افزودن پنل</Button>
      </Stack>
      <Card>
        <Table>
          <TableHead>
            <TableRow>
              <SortTh id="key" label="کلید" sortKey={key} dir={dir} onSort={toggle} />
              <SortTh id="name" label="نام" sortKey={key} dir={dir} onSort={toggle} />
              <SortTh id="host" label="دامنه" sortKey={key} dir={dir} onSort={toggle} />
              <SortTh id="status" label="وضعیت" sortKey={key} dir={dir} onSort={toggle} />
              <SortTh id="resellers_count" label="نمایندگان" sortKey={key} dir={dir} onSort={toggle} />
              <SortTh id="end_users_count" label="کاربران" sortKey={key} dir={dir} onSort={toggle} />
              <SortTh id="last_synced_at" label="آخرین همگام‌سازی" sortKey={key} dir={dir} onSort={toggle} />
              <TableCell align="left">عملیات</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {sorted.map((p: any) => (
              <TableRow key={p.id} hover>
                <TableCell>{p.key}</TableCell>
                <TableCell>{p.name}</TableCell>
                <TableCell dir="ltr">{p.host}</TableCell>
                <TableCell>{statusChip(p.status)}</TableCell>
                <TableCell>{fmtNum(p.resellers_count)}</TableCell>
                <TableCell>{fmtNum(p.end_users_count)}</TableCell>
                <TableCell>{fmtDate(p.last_synced_at)}</TableCell>
                <TableCell align="left">
                  <Tooltip title="همگام‌سازی"><IconButton onClick={() => doSync.mutate(p.id)}><SyncIcon /></IconButton></Tooltip>
                  <Tooltip title="تست اتصال"><IconButton onClick={() => doTest.mutate(p.id)}><WifiTetheringIcon /></IconButton></Tooltip>
                  <Tooltip title="ویرایش"><IconButton onClick={() => edit(p)}><EditIcon /></IconButton></Tooltip>
                  <Tooltip title="حذف"><IconButton color="error" onClick={() => confirm("حذف این پنل؟") && doDelete.mutate(p.id)}><DeleteIcon /></IconButton></Tooltip>
                </TableCell>
              </TableRow>
            ))}
            {!isLoading && data.length === 0 && (
              <TableRow><TableCell colSpan={8} align="center" sx={{ py: 4, color: "text.secondary" }}>پنلی ثبت نشده است</TableCell></TableRow>
            )}
          </TableBody>
        </Table>
      </Card>

      <Dialog open={open} onClose={() => setOpen(false)} fullWidth maxWidth="sm">
        <DialogTitle>{form.id ? "ویرایش پنل" : "افزودن پنل"}</DialogTitle>
        <DialogContent>
          <Stack spacing={2} sx={{ mt: 1 }}>
            <TextField
              label="لینک پنل را اینجا بچسبانید (پر شدن خودکار)"
              dir="ltr"
              value={link}
              onChange={(e) => applyLink(e.target.value)}
              placeholder="https://host/proxy_path/owner_uuid/admin/adminuser/"
              helperText="دامنه، مسیر مخفی و UUID به‌صورت خودکار از روی لینک پر می‌شوند؛ بقیه را در صورت نیاز اصلاح کنید."
            />
            <TextField label="کلید (مثل fa1)" value={form.key} disabled={!!form.id}
              onChange={(e) => setForm({ ...form, key: e.target.value })} />
            <TextField label="نام" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
            <TextField label="دامنه (بدون https)" dir="ltr" value={form.host} onChange={(e) => setForm({ ...form, host: e.target.value })} />
            <TextField label={form.id ? "مسیر مخفی (برای تغییر وارد کنید)" : "مسیر مخفی (proxy path)"} dir="ltr"
              value={form.proxy_path} onChange={(e) => setForm({ ...form, proxy_path: e.target.value })} />
            <TextField label="UUID مالک پنل (Owner)" dir="ltr" value={form.owner_uuid} onChange={(e) => setForm({ ...form, owner_uuid: e.target.value })} />
            <TextField label="کلید API ادمین (برای مسدودسازی)" dir="ltr" value={form.admin_api_key}
              onChange={(e) => setForm({ ...form, admin_api_key: e.target.value })}
              helperText="برای اجرای واقعی مسدودسازی لازم است" />
            <FormControlLabel control={<Switch checked={form.enabled} onChange={(e) => setForm({ ...form, enabled: e.target.checked })} />} label="فعال" />
          </Stack>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setOpen(false)}>انصراف</Button>
          <Button variant="contained" onClick={() => save.mutate()} disabled={save.isPending}>ذخیره</Button>
        </DialogActions>
      </Dialog>
      {node}
    </Box>
  );
}
