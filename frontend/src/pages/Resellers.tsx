import { useState } from "react";
import {
  Box, Button, Card, Chip, Collapse, Dialog, DialogActions, DialogContent, DialogTitle,
  IconButton, MenuItem, Stack, Switch, FormControlLabel, Tab, Tabs, Table, TableBody,
  TableCell, TableHead, TableRow, TextField, Tooltip, TablePagination, Typography,
} from "@mui/material";
import { alpha } from "@mui/material/styles";
import EditIcon from "@mui/icons-material/Edit";
import BlockIcon from "@mui/icons-material/Block";
import RestartAltIcon from "@mui/icons-material/RestartAlt";
import AddIcon from "@mui/icons-material/Add";
import KeyboardArrowDownIcon from "@mui/icons-material/KeyboardArrowDown";
import KeyboardArrowLeftIcon from "@mui/icons-material/KeyboardArrowLeft";
import SubdirectoryArrowLeftIcon from "@mui/icons-material/SubdirectoryArrowLeft";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  listResellers, getResellerTree, listPanels, updateReseller, enforceReseller, restoreReseller,
  bumpResellerLimits, setResellerCanAddAdmin,
} from "../api/client";
import { useToast, errMsg } from "../components/Toast";
import { useSort, SortTh } from "../components/sortable";
import CapacityBar from "../components/CapacityBar";
import { fmtNum } from "../format";

const ENF_FA: any = { active: ["فعال", "success"], warned: ["اخطار", "warning"], enforced: ["مسدود", "error"] };

function enfChip(state: string) {
  const [lbl, color] = ENF_FA[state] || [state, "default"];
  return <Chip size="small" color={color as any} label={lbl} />;
}

export default function Resellers() {
  const qc = useQueryClient();
  const { node, show } = useToast();
  const [tab, setTab] = useState(0);
  const [panelId, setPanelId] = useState<string>("");
  const [q, setQ] = useState("");
  const [page, setPage] = useState(0);
  const [form, setForm] = useState<any>(null);
  const [bumpRow, setBumpRow] = useState<any>(null);
  const [bumpAmount, setBumpAmount] = useState(100);

  const { data: panels = [] } = useQuery({ queryKey: ["panels"], queryFn: listPanels });
  // List view shows only MAIN (top-level) resellers — sub-resellers live in the tree tab.
  const { data = [] } = useQuery({
    queryKey: ["resellers", panelId, q],
    queryFn: () => listResellers({ panel_id: panelId || undefined, q: q || undefined, top_level_only: true, limit: 2000 }),
  });
  const billableCount = data.filter((r: any) => !r.exclude_from_billing).length;
  const exemptCount = data.filter((r: any) => r.exclude_from_billing).length;
  const { data: tree = [] } = useQuery({
    queryKey: ["reseller-tree", panelId, q],
    queryFn: () => getResellerTree({ panel_id: panelId || undefined, q: q || undefined }),
    enabled: tab === 1,
  });
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["resellers"] });
    qc.invalidateQueries({ queryKey: ["reseller-tree"] });
  };

  const save = useMutation({
    mutationFn: () => updateReseller(form.id, {
      price_per_gb: form.price_per_gb ? Number(form.price_per_gb) : null,
      // Empty = "use the global default"; an explicit 0 = "no minimum-sale floor". Treating
      // 0 as falsy here used to silently revert it to the default, so distinguish "" from 0.
      min_sale_toman:
        form.min_sale_toman === "" || form.min_sale_toman == null
          ? null
          : Number(form.min_sale_toman),
      exclude_from_billing: form.exclude_from_billing,
    }),
    onSuccess: () => { show("ذخیره شد"); setForm(null); refresh(); },
    onError: (e) => show(errMsg(e), "error"),
  });
  const enforce = useMutation({
    mutationFn: (id: number) => enforceReseller(id),
    onSuccess: (r) => { show(r.dry_run ? `حالت آزمایشی: ${r.affected_users} کاربر` : `مسدود شد: ${r.affected_users} کاربر`, r.dry_run ? "info" : "success"); refresh(); },
    onError: (e) => show(errMsg(e), "error"),
  });
  const restore = useMutation({
    mutationFn: (id: number) => restoreReseller(id),
    onSuccess: (r) => { show(`بازگردانی: ${r.status}`); refresh(); },
    onError: (e) => show(errMsg(e), "error"),
  });
  const bump = useMutation({
    mutationFn: ({ id, amount }: { id: number; amount: number }) => bumpResellerLimits(id, amount),
    onSuccess: (r) => { show(`ظرفیت افزایش یافت → سقف کاربران: ${r.max_users}`); setBumpRow(null); refresh(); },
    onError: (e) => show(errMsg(e), "error"),
  });
  const canAdd = useMutation({
    mutationFn: ({ id, enabled }: { id: number; enabled: boolean }) => setResellerCanAddAdmin(id, enabled),
    onSuccess: (r) => { show(r.can_add_admin ? "ساخت زیرمجموعه فعال شد" : "ساخت زیرمجموعه غیرفعال شد"); refresh(); },
    onError: (e) => show(errMsg(e), "error"),
  });

  const { sorted, key, dir, toggle } = useSort(data, "name", "asc");
  const rows = sorted.slice(page * 25, page * 25 + 25);

  const actions = (r: any) => (
    <>
      <Tooltip title="ویرایش"><IconButton size="small" onClick={() => setForm({ ...r })}><EditIcon fontSize="small" /></IconButton></Tooltip>
      <Tooltip title="افزایش ظرفیت کاربران"><IconButton size="small" color="primary" onClick={() => { setBumpAmount(100); setBumpRow(r); }}><AddIcon fontSize="small" /></IconButton></Tooltip>
      {r.enforcement_state === "enforced" ? (
        <Tooltip title="بازگردانی"><IconButton size="small" color="success" onClick={() => restore.mutate(r.id)}><RestartAltIcon fontSize="small" /></IconButton></Tooltip>
      ) : (
        <Tooltip title="مسدودسازی"><IconButton size="small" color="error" onClick={() => confirm("مسدودسازی این نماینده؟ (در حالت آزمایشی فقط ثبت می‌شود)") && enforce.mutate(r.id)}><BlockIcon fontSize="small" /></IconButton></Tooltip>
      )}
    </>
  );

  // The "can create sub-admins" toggle (writes to the panel immediately).
  const canAddSwitch = (r: any) => (
    <Tooltip title={r.can_add_admin ? "اجازهٔ ساخت زیرمجموعه دارد — برای خاموش‌کردن بزنید" : "بدون اجازهٔ ساخت زیرمجموعه — برای روشن‌کردن بزنید"}>
      <Switch size="small" checked={!!r.can_add_admin} disabled={canAdd.isPending}
        onChange={(e) => canAdd.mutate({ id: r.id, enabled: e.target.checked })} />
    </Tooltip>
  );

  return (
    <Box>
      <Stack direction={{ xs: "column", sm: "row" }} spacing={2} sx={{ mb: 2 }}>
        <TextField select size="small" label="پنل" value={panelId} sx={{ minWidth: 160 }}
          onChange={(e) => { setPanelId(e.target.value); setPage(0); }}>
          <MenuItem value="">همه پنل‌ها</MenuItem>
          {panels.map((p: any) => <MenuItem key={p.id} value={p.id}>{p.key}</MenuItem>)}
        </TextField>
        <TextField size="small" label="جستجوی نام" value={q} onChange={(e) => { setQ(e.target.value); setPage(0); }} />
      </Stack>

      <Tabs value={tab} onChange={(_, v) => setTab(v)} sx={{ mb: 2 }}>
        <Tab label="فهرست (نماینده‌های اصلی)" />
        <Tab label="ساختار درختی (نماینده و زیرمجموعه‌ها)" />
      </Tabs>

      {tab === 0 ? (
        <Card>
          <Box sx={{ p: 2, pb: 0 }}>
            <Typography variant="body2" color="text.secondary">
              {fmtNum(billableCount)} نمایندهٔ اصلی
              {exemptCount ? <>{" "}(<Box component="span" sx={{ color: "warning.main" }}>{fmtNum(exemptCount)} معاف از فاکتور</Box>)</> : ""}
              {" "}— زیرمجموعه‌ها در تب «ساختار درختی» نمایش داده می‌شوند.
            </Typography>
          </Box>
          <Table size="small">
            <TableHead>
              <TableRow>
                <SortTh id="name" label="نام" sortKey={key} dir={dir} onSort={toggle} />
                <SortTh id="panel_key" label="پنل" sortKey={key} dir={dir} onSort={toggle} />
                <SortTh id="effective_price_per_gb" label="قیمت/گیگ" sortKey={key} dir={dir} onSort={toggle} />
                <SortTh id="capacity_pct" label="پُری ظرفیت" sortKey={key} dir={dir} onSort={toggle} />
                <SortTh id="can_add_admin" label="ساخت زیرمجموعه" sortKey={key} dir={dir} onSort={toggle} />
                <SortTh id="registered" label="ربات" sortKey={key} dir={dir} onSort={toggle} />
                <SortTh id="enforcement_state" label="وضعیت" sortKey={key} dir={dir} onSort={toggle} />
                <SortTh id="exclude_from_billing" label="محاسبه" sortKey={key} dir={dir} onSort={toggle} />
                <TableCell align="left">عملیات</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {rows.map((r: any) => (
                <TableRow key={r.id} hover>
                  <TableCell>{r.name}</TableCell>
                  <TableCell>{r.panel_key}</TableCell>
                  <TableCell>{fmtNum(r.effective_price_per_gb)}{r.price_per_gb ? "" : " (پیش‌فرض)"}</TableCell>
                  <TableCell><CapacityBar used={r.users_count} max={r.panel_max_users} /></TableCell>
                  <TableCell>{canAddSwitch(r)}</TableCell>
                  <TableCell>{r.registered ? <Chip size="small" color="success" label="متصل" /> : <Chip size="small" label="—" />}</TableCell>
                  <TableCell>{enfChip(r.enforcement_state)}</TableCell>
                  <TableCell>{r.exclude_from_billing ? <Chip size="small" color="warning" label="معاف" /> : "✓"}</TableCell>
                  <TableCell align="left">{actions(r)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
          <TablePagination component="div" count={data.length} page={page} rowsPerPage={25}
            rowsPerPageOptions={[25]} onPageChange={(_, p) => setPage(p)} labelDisplayedRows={({ from, to, count }) => `${from}–${to} از ${count}`} />
        </Card>
      ) : (
        <Card>
          <Box sx={{ p: 2, pb: 0 }}>
            <Typography variant="body2" color="text.secondary">
              {fmtNum(tree.length)} نمایندهٔ اصلی — زیرمجموعه‌ها داخل هر نماینده نمایش داده می‌شوند.
            </Typography>
          </Box>
          <Table size="small">
            <TableHead>
              <TableRow>
                <TableCell>نماینده</TableCell>
                <TableCell>پنل</TableCell>
                <TableCell>قیمت/گیگ</TableCell>
                <TableCell>پُری ظرفیت</TableCell>
                <TableCell>ساخت زیرمجموعه</TableCell>
                <TableCell>ربات</TableCell>
                <TableCell>وضعیت</TableCell>
                <TableCell align="left">عملیات</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {tree.map((r: any) => (
                <TreeRow key={r.id} node={r} depth={0} actions={actions} canAddSwitch={canAddSwitch} />
              ))}
              {tree.length === 0 && (
                <TableRow><TableCell colSpan={8} align="center" sx={{ py: 4, color: "text.secondary" }}>داده‌ای نیست</TableCell></TableRow>
              )}
            </TableBody>
          </Table>
        </Card>
      )}

      <Dialog open={!!form} onClose={() => setForm(null)} fullWidth maxWidth="xs">
        <DialogTitle>ویرایش نماینده {form?.name}</DialogTitle>
        <DialogContent>
          <Stack spacing={2} sx={{ mt: 1 }}>
            <TextField label="قیمت هر گیگ (تومان) — خالی = پیش‌فرض" type="number" value={form?.price_per_gb ?? ""}
              onChange={(e) => setForm({ ...form, price_per_gb: e.target.value })} />
            <TextField label="حداقل فروش (تومان) — خالی = پیش‌فرض، ۰ = بدون حداقل" type="number" value={form?.min_sale_toman ?? ""}
              onChange={(e) => setForm({ ...form, min_sale_toman: e.target.value })}
              helperText="برای کل مجموعهٔ این نماینده (خودش + زیرمجموعه‌ها) اعمال می‌شود" />
            <FormControlLabel control={<Switch checked={!!form?.exclude_from_billing}
              onChange={(e) => setForm({ ...form, exclude_from_billing: e.target.checked })} />} label="معاف از صدور فاکتور" />
          </Stack>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setForm(null)}>انصراف</Button>
          <Button variant="contained" onClick={() => save.mutate()}>ذخیره</Button>
        </DialogActions>
      </Dialog>

      <Dialog open={!!bumpRow} onClose={() => setBumpRow(null)} fullWidth maxWidth="xs">
        {bumpRow && (<>
          <DialogTitle>افزایش ظرفیت — {bumpRow.name}</DialogTitle>
          <DialogContent>
            <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
              این مقدار به هر دو سقفِ «تعداد کاربران» و «کاربران فعال» این نماینده روی پنل اضافه می‌شود.
              {bumpRow.panel_max_users != null && (
                <> سقف فعلی: {fmtNum(bumpRow.panel_max_users)} (ساخته: {fmtNum(bumpRow.users_count)}).</>
              )}
            </Typography>
            <Stack direction="row" spacing={1} sx={{ mb: 2 }}>
              {[50, 100, 200, 500].map((n) => (
                <Button key={n} size="small" variant={bumpAmount === n ? "contained" : "outlined"}
                  onClick={() => setBumpAmount(n)}>+{n}</Button>
              ))}
            </Stack>
            <TextField type="number" label="مقدار افزایش" fullWidth value={bumpAmount}
              onChange={(e) => setBumpAmount(Math.max(1, Number(e.target.value) || 0))} />
          </DialogContent>
          <DialogActions>
            <Button onClick={() => setBumpRow(null)}>انصراف</Button>
            <Button variant="contained" disabled={bump.isPending || bumpAmount < 1}
              onClick={() => bump.mutate({ id: bumpRow.id, amount: bumpAmount })}>
              افزودن +{bumpAmount}
            </Button>
          </DialogActions>
        </>)}
      </Dialog>
      {node}
    </Box>
  );
}

function TreeRow({ node, depth, actions, canAddSwitch }: { node: any; depth: number; actions: (r: any) => any; canAddSwitch: (r: any) => any }) {
  const [open, setOpen] = useState(depth === 0);
  const hasKids = (node.children?.length || 0) > 0;
  return (
    <>
      <TableRow hover sx={depth > 0 ? { bgcolor: (t) => alpha(t.palette.primary.main, 0.03) } : undefined}>
        <TableCell>
          <Box sx={{ display: "flex", alignItems: "center", pr: depth * 3 }}>
            {hasKids ? (
              <IconButton size="small" onClick={() => setOpen((o) => !o)} sx={{ ml: 0.5 }}>
                {open ? <KeyboardArrowDownIcon fontSize="small" /> : <KeyboardArrowLeftIcon fontSize="small" />}
              </IconButton>
            ) : (
              depth > 0 && <SubdirectoryArrowLeftIcon fontSize="small" sx={{ color: "text.disabled", ml: 0.5 }} />
            )}
            <Box>
              <Typography variant="body2" sx={{ fontWeight: depth === 0 ? 700 : 400 }}>
                {node.name}
              </Typography>
              {hasKids && (
                <Typography variant="caption" color="text.secondary">
                  {fmtNum(node.descendant_count)} زیرمجموعه
                </Typography>
              )}
            </Box>
          </Box>
        </TableCell>
        <TableCell>{node.panel_key}</TableCell>
        <TableCell>{fmtNum(node.effective_price_per_gb)}</TableCell>
        <TableCell><CapacityBar used={node.users_count} max={node.panel_max_users} /></TableCell>
        <TableCell>{canAddSwitch(node)}</TableCell>
        <TableCell>{node.registered ? <Chip size="small" color="success" label="متصل" /> : <Chip size="small" label="—" />}</TableCell>
        <TableCell>{enfChip(node.enforcement_state)}</TableCell>
        <TableCell align="left">{actions(node)}</TableCell>
      </TableRow>
      {hasKids && open && node.children.map((c: any) => (
        <TreeRow key={c.id} node={c} depth={depth + 1} actions={actions} canAddSwitch={canAddSwitch} />
      ))}
    </>
  );
}
