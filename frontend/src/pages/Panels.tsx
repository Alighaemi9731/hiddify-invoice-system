import { useState } from "react";
import {
  Box, Button, Card, Chip, Dialog, DialogActions, DialogContent, DialogTitle, Divider,
  IconButton, Stack, Table, TableBody, TableCell, TableHead, TableRow, TextField,
  Tooltip, Switch, FormControlLabel, Typography,
} from "@mui/material";
import SyncIcon from "@mui/icons-material/esm/Sync";
import EditIcon from "@mui/icons-material/esm/Edit";
import DeleteIcon from "@mui/icons-material/esm/Delete";
import WifiTetheringIcon from "@mui/icons-material/esm/WifiTethering";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  listPanels, createPanel, updatePanel, deletePanel, syncPanel, syncAllPanels, testPanel,
} from "../api/client";
import { useToast } from "../components/Toast";
import { useToastMutation } from "../hooks/useToastMutation";
import { useSort, SortTh } from "../components/sortable";
import { fmtNum, fmtDateTime } from "../format";

const EMPTY = { key: "", name: "", host: "", proxy_path: "", owner_uuid: "", admin_api_key: "", enabled: true, host_aliases: [] as string[] };

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
  const [migrateHost, setMigrateHost] = useState("");
  const refresh = () => qc.invalidateQueries({ queryKey: ["panels"] });

  // Domain move in one save: the current host drops into the aliases (so old reseller links keep
  // matching in the bot) and the new host replaces it (host change → normal re-sync).
  const migrate = useToastMutation({
    show,
    mutationFn: () => {
      const newHost = migrateHost.trim();
      const aliases = Array.from(new Set([...(form.host_aliases || []), form.host].filter(Boolean)));
      return updatePanel(form.id, { host: newHost, host_aliases: aliases });
    },
    success: "دامنه منتقل شد؛ هاستِ قبلی برای تطبیقِ لینک‌ها حفظ شد.",
    onSuccess: () => setOpen(false),
    invalidate: ["panels"],
  });

  const save = useToastMutation({
    show,
    mutationFn: () => {
      // On edit, don't overwrite the stored secret fields with blanks.
      const payload: any = { ...form };
      if (form.id) {
        if (!payload.proxy_path) delete payload.proxy_path;
        if (!payload.admin_api_key) delete payload.admin_api_key;
      }
      return form.id ? updatePanel(form.id, payload) : createPanel(payload);
    },
    success: "ذخیره شد",
    onSuccess: () => setOpen(false),
    invalidate: ["panels"],
  });

  const applyLink = (value: string) => {
    setLink(value);
    const p = parsePanelLink(value);
    if (p) setForm((f: any) => ({
      ...f, host: p.host, proxy_path: p.proxy_path, owner_uuid: p.owner_uuid,
      key: f.key || p.key, name: f.name || p.key,
    }));
  };
  const doSync = useToastMutation({
    show,
    mutationFn: (id: number) => syncPanel(id),
    success: ["همگام‌سازی آغاز شد؛ نتیجه تا چند لحظه دیگر به‌روزرسانی می‌شود.", "info"],
    // Poll a few times so the status flips to «موفق» without a manual reload.
    onSuccess: () => [4000, 9000, 16000, 25000].forEach((ms) => setTimeout(refresh, ms)),
    invalidate: ["panels"],
  });
  const doTest = useToastMutation({
    show,
    mutationFn: (id: number) => testPanel(id),
    success: (r): [string, "success" | "error"] =>
      [r.ok ? `اتصال موفق — ${r.admin_count} ادمین / ${r.user_count} کاربر` : `ناموفق: ${r.error}`, r.ok ? "success" : "error"],
  });
  const doSyncAll = useToastMutation({
    show,
    mutationFn: () => syncAllPanels(),
    success: ["همگام‌سازی همهٔ پنل‌ها آغاز شد؛ نتیجه تا چند لحظه دیگر به‌روزرسانی می‌شود.", "info"],
    onSuccess: () => [4000, 9000, 16000, 25000].forEach((ms) => setTimeout(refresh, ms)),
    invalidate: ["panels"],
  });
  // Deleting a billed panel permanently removes hundreds of invoice rows (and can strand a
  // cross-panel payment). The financial ledger survives, but a delete that large should state its
  // scope: the API answers 409 with the exact counts, we re-ask, then retry with force.
  const doDelete = useToastMutation({
    show,
    mutationFn: async (id: number) => {
      try {
        return await deletePanel(id);
      } catch (e: any) {
        const detail = e?.response?.data?.detail;
        if (e?.response?.status === 409 && detail && confirm(`${detail}\n\nحذف اجباری انجام شود؟`)) {
          return await deletePanel(id, true);
        }
        throw e;
      }
    },
    success: "حذف شد",
    invalidate: ["panels"],
  });

  const { sorted, key, dir, toggle } = useSort(data, "key", "asc");
  const edit = (p?: any) => {
    setForm(p ? { ...p, proxy_path: "", admin_api_key: "", host_aliases: p.host_aliases || [] } : EMPTY);
    setLink(""); setMigrateHost(""); setOpen(true);
  };

  const statusChip = (s: string) => {
    const map: any = { ok: ["موفق", "success"], error: ["خطا", "error"], unknown: ["در حال همگام‌سازی…", "info"], disabled: ["غیرفعال", "warning"] };
    const [lbl, color] = map[s] || [s, "default"];
    return <Chip size="small" label={lbl} color={color} />;
  };

  return (
    <Box>
      <Stack direction={{ xs: "column", sm: "row" }} justifyContent="space-between"
        alignItems={{ xs: "stretch", sm: "center" }} spacing={1.5} sx={{ mb: 2 }}>
        <Typography variant="body2" color="text.secondary">
          پنل‌های هیدیفای متصل
        </Typography>
        <Stack direction="row" spacing={1}>
          <Button variant="outlined" startIcon={<SyncIcon />} disabled={doSyncAll.isPending}
            onClick={() => doSyncAll.mutate()}>همگام‌سازی همه</Button>
          <Button variant="contained" onClick={() => edit()}>افزودن پنل</Button>
        </Stack>
      </Stack>
      <Card>
        <Table className="resp-table">
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
                <TableCell dir="ltr">{fmtDateTime(p.last_synced_at)}</TableCell>
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
            {/* Normalized to lowercase as you type so what you see is what gets saved
                (the backend applies the same rule). Locked after creation. */}
            <TextField label="کلید (مثل fa1)" inputProps={{ dir: "ltr" }} value={form.key} disabled={!!form.id}
              onChange={(e) => setForm({ ...form, key: e.target.value.trimStart().toLowerCase() })}
              helperText={form.id
                ? "کلید پس از ساخت قابل تغییر نیست (در تاریخچهٔ مالی ثبت شده است)."
                : "شناسهٔ کوتاه و یکتای پنل؛ فقط با حروف کوچک ذخیره می‌شود."} />
            <TextField label="نام" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
            <TextField label="دامنه (بدون https)" inputProps={{ dir: "ltr" }} value={form.host} onChange={(e) => setForm({ ...form, host: e.target.value })} />
            <TextField label={form.id ? "مسیر مخفی (برای تغییر وارد کنید)" : "مسیر مخفی (proxy path)"} inputProps={{ dir: "ltr" }}
              value={form.proxy_path} onChange={(e) => setForm({ ...form, proxy_path: e.target.value })} />
            <TextField label="UUID مالک پنل (Owner)" inputProps={{ dir: "ltr" }} value={form.owner_uuid} onChange={(e) => setForm({ ...form, owner_uuid: e.target.value })} />
            <TextField label="کلید API ادمین (برای مسدودسازی)" inputProps={{ dir: "ltr" }} value={form.admin_api_key}
              onChange={(e) => setForm({ ...form, admin_api_key: e.target.value })}
              helperText="برای اجرای واقعی مسدودسازی لازم است" />
            <TextField label="هاست‌های قبلی/مستعار (هر خط یک هاست)" inputProps={{ dir: "ltr" }}
              multiline minRows={2}
              value={(form.host_aliases || []).join("\n")}
              onChange={(e) => setForm({ ...form, host_aliases: e.target.value.split(/[\n,]+/).map((s: string) => s.trim()).filter(Boolean) })}
              helperText="هاستِ اصلی برای بکاپ/همگام‌سازی استفاده می‌شود؛ هاست‌های قبلی فقط برای اینکه لینک‌های قدیمیِ نماینده‌ها همچنان در ربات ثبت شوند. (تغییرِ این فیلد همگام‌سازیِ مجدد نمی‌زند.)" />
            <FormControlLabel control={<Switch checked={form.enabled} onChange={(e) => setForm({ ...form, enabled: e.target.checked })} />} label="فعال" />

            {form.id && (
              <>
                <Divider />
                <Typography variant="subtitle2">مهاجرتِ دامنه</Typography>
                <Typography variant="caption" color="text.secondary">
                  دامنهٔ جدید را وارد کنید؛ هاستِ فعلی («{form.host}») به فهرستِ «هاست‌های قبلی» منتقل و دامنهٔ جدید جایگزین می‌شود — در یک مرحله.
                </Typography>
                <Stack direction="row" spacing={1} alignItems="center">
                  <TextField size="small" fullWidth label="دامنهٔ جدید (بدون https)" inputProps={{ dir: "ltr" }}
                    value={migrateHost} onChange={(e) => setMigrateHost(e.target.value)} />
                  <Button variant="outlined" color="warning" disabled={!migrateHost.trim() || migrate.isPending}
                    onClick={() => migrate.mutate()}>انتقال و ذخیره</Button>
                </Stack>
              </>
            )}
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
