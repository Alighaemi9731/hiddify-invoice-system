import { MouseEvent, ReactNode, useEffect, useMemo, useState } from "react";
import {
  Box,
  Button,
  Card,
  Chip,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  FormControlLabel,
  IconButton,
  InputAdornment,
  MenuItem,
  Skeleton,
  Stack,
  Switch,
  Tab,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TablePagination,
  TableRow,
  Tabs,
  TextField,
  Tooltip,
  Typography,
  useMediaQuery,
} from "@mui/material";
import { alpha, useTheme } from "@mui/material/styles";
import AccountTreeIcon from "@mui/icons-material/esm/AccountTree";
import AddIcon from "@mui/icons-material/esm/Add";
import BlockIcon from "@mui/icons-material/esm/Block";
import CheckCircleOutlineIcon from "@mui/icons-material/esm/CheckCircleOutline";
import EditIcon from "@mui/icons-material/esm/Edit";
import FormatListBulletedIcon from "@mui/icons-material/esm/FormatListBulleted";
import KeyboardArrowDownIcon from "@mui/icons-material/esm/KeyboardArrowDown";
import KeyboardArrowLeftIcon from "@mui/icons-material/esm/KeyboardArrowLeft";
import RestartAltIcon from "@mui/icons-material/esm/RestartAlt";
import SearchIcon from "@mui/icons-material/esm/Search";
import WarningAmberIcon from "@mui/icons-material/esm/WarningAmber";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  bumpResellerLimits,
  enforceReseller,
  getResellerTree,
  listPanels,
  listResellers,
  ResellerRow,
  ResellerTreeRow,
  restoreReseller,
  setResellerCanAddAdmin,
  updateReseller,
} from "../api/client";
import CapacityBar from "../components/CapacityBar";
import { Dir, SortTh, useSort } from "../components/sortable";
import { errMsg, useToast } from "../components/Toast";
import { fmtNum } from "../format";

type VisibleTreeRow = { node: ResellerTreeRow; depth: number };

const countTree = (nodes: ResellerTreeRow[]): number =>
  nodes.reduce((total, node) => total + 1 + countTree(node.children || []), 0);

const branchIds = (nodes: ResellerTreeRow[]): number[] =>
  nodes.flatMap((node) => [
    ...(node.children?.length ? [node.id] : []),
    ...branchIds(node.children || []),
  ]);

function flattenVisible(
  nodes: ResellerTreeRow[],
  expanded: Set<number>,
  depth = 0,
): VisibleTreeRow[] {
  const rows: VisibleTreeRow[] = [];
  for (const node of nodes) {
    rows.push({ node, depth });
    if (expanded.has(node.id) && node.children?.length) {
      rows.push(...flattenVisible(node.children, expanded, depth + 1));
    }
  }
  return rows;
}

function compareRows(a: ResellerRow, b: ResellerRow, key: string, dir: Dir) {
  const av = (a as any)[key];
  const bv = (b as any)[key];
  if (av == null && bv == null) return 0;
  if (av == null) return 1;
  if (bv == null) return -1;

  let result: number;
  if (typeof av === "number" && typeof bv === "number") result = av - bv;
  else if (typeof av === "boolean" && typeof bv === "boolean") {
    result = Number(av) - Number(bv);
  } else {
    result = String(av).localeCompare(String(bv), "fa");
  }
  return dir === "asc" ? result : -result;
}

function sortTree(
  nodes: ResellerTreeRow[],
  key: string,
  dir: Dir,
): ResellerTreeRow[] {
  return [...nodes]
    .sort((a, b) => compareRows(a, b, key, dir))
    .map((node) => ({
      ...node,
      children: sortTree(node.children || [], key, dir),
    }));
}

function StatusPill({
  children,
  color,
  muted = false,
}: {
  children: ReactNode;
  color: string;
  muted?: boolean;
}) {
  return (
    <Box
      component="span"
      sx={{
        display: "inline-flex",
        alignItems: "center",
        gap: 0.7,
        px: 1.1,
        py: 0.55,
        borderRadius: 99,
        color: muted ? "text.secondary" : color,
        bgcolor: (theme) => alpha(color, muted ? 0.05 : theme.palette.mode === "dark" ? 0.16 : 0.09),
        border: "1px solid",
        borderColor: (theme) => alpha(color, muted ? 0.12 : theme.palette.mode === "dark" ? 0.34 : 0.22),
        fontSize: 12,
        fontWeight: 750,
        lineHeight: 1,
        whiteSpace: "nowrap",
      }}
    >
      <Box sx={{ width: 7, height: 7, borderRadius: "50%", bgcolor: "currentColor" }} />
      {children}
    </Box>
  );
}

function ConnectionStatus({ connected }: { connected: boolean }) {
  return connected ? (
    <StatusPill color="#10b981">متصل</StatusPill>
  ) : (
    <StatusPill color="#94a3b8" muted>متصل نیست</StatusPill>
  );
}

function EnforcementStatus({ state }: { state: string }) {
  return state === "enforced" ? (
    <StatusPill color="#f43f5e">مسدود</StatusPill>
  ) : (
    <StatusPill color="#10b981">فعال</StatusPill>
  );
}

export default function Resellers() {
  const theme = useTheme();
  const isMobile = useMediaQuery(theme.breakpoints.down("md"));
  const qc = useQueryClient();
  const { node: toastNode, show } = useToast();
  const [tab, setTab] = useState(0);
  const [panelId, setPanelId] = useState("");
  const [q, setQ] = useState("");
  const [page, setPage] = useState(0);
  const [form, setForm] = useState<ResellerRow | null>(null);
  const [bumpRow, setBumpRow] = useState<ResellerRow | null>(null);
  const [bumpAmount, setBumpAmount] = useState(100);
  const [expanded, setExpanded] = useState<Set<number>>(new Set());

  const { data: panels = [] } = useQuery({
    queryKey: ["panels"],
    queryFn: listPanels,
  });
  const { data = [], isLoading: listLoading } = useQuery({
    queryKey: ["resellers", panelId, q],
    queryFn: () => listResellers({
      panel_id: panelId || undefined,
      q: q || undefined,
      top_level_only: true,
      limit: 2000,
    }),
  });
  const { data: tree = [], isLoading: treeLoading } = useQuery({
    queryKey: ["reseller-tree", panelId, q],
    queryFn: () => getResellerTree({
      panel_id: panelId || undefined,
      q: q || undefined,
    }),
    enabled: tab === 1,
  });

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["resellers"] });
    qc.invalidateQueries({ queryKey: ["reseller-tree"] });
  };

  const save = useMutation({
    mutationFn: () => {
      if (!form) throw new Error("نماینده‌ای انتخاب نشده است");
      return updateReseller(form.id, {
        price_per_gb: form.price_per_gb ? Number(form.price_per_gb) : null,
        min_sale_toman:
          form.min_sale_toman === ("" as any) || form.min_sale_toman == null
            ? null
            : Number(form.min_sale_toman),
        exclude_from_billing: form.exclude_from_billing,
      });
    },
    onSuccess: () => {
      show("ذخیره شد");
      setForm(null);
      refresh();
    },
    onError: (error) => show(errMsg(error), "error"),
  });
  const enforce = useMutation({
    mutationFn: (id: number) => enforceReseller(id),
    onSuccess: (result) => {
      show(
        result.dry_run
          ? `حالت آزمایشی: ${result.affected_users} کاربر`
          : result.queued
            ? "مسدودسازی در صف ثبت شد"
            : "این نماینده از قبل مسدود است",
        result.dry_run || result.queued ? "info" : "success",
      );
      refresh();
    },
    onError: (error) => show(errMsg(error), "error"),
  });
  const restore = useMutation({
    mutationFn: (id: number) => restoreReseller(id),
    onSuccess: (result) => {
      show(
        result.queued
          ? "آزادسازی در صف ثبت شد"
          : result.status === "not_enforced"
            ? "این نماینده مسدود نیست"
            : `بازگردانی: ${result.status}`,
        result.queued ? "info" : "success",
      );
      refresh();
    },
    onError: (error) => show(errMsg(error), "error"),
  });
  const bump = useMutation({
    mutationFn: ({ id, amount }: { id: number; amount: number }) =>
      bumpResellerLimits(id, amount),
    onSuccess: (result) => {
      show(`ظرفیت افزایش یافت → سقف کاربران: ${result.max_users}`);
      setBumpRow(null);
      refresh();
    },
    onError: (error) => show(errMsg(error), "error"),
  });
  const canAdd = useMutation({
    mutationFn: ({ id, enabled }: { id: number; enabled: boolean }) =>
      setResellerCanAddAdmin(id, enabled),
    onSuccess: (result) => {
      show(result.can_add_admin ? "ساخت زیرمجموعه فعال شد" : "ساخت زیرمجموعه غیرفعال شد");
      refresh();
    },
    onError: (error) => show(errMsg(error), "error"),
  });

  const { sorted, key, dir, toggle } = useSort(data, "name", "asc");
  const rows = sorted.slice(page * 25, page * 25 + 25);
  const sortedTree = useMemo(
    () => sortTree(tree, key, dir),
    [tree, key, dir],
  );
  const pagedTree = useMemo(
    () => sortedTree.slice(page * 25, page * 25 + 25),
    [sortedTree, page],
  );
  const visibleTreeRows = useMemo(
    () => flattenVisible(pagedTree, expanded),
    [pagedTree, expanded],
  );
  const allBranchIds = useMemo(() => branchIds(pagedTree), [pagedTree]);
  const billableCount = data.filter((item) => !item.exclude_from_billing).length;
  const exemptCount = data.length - billableCount;
  const treeCount = countTree(tree);
  const currentCount = tab === 0 ? data.length : treeCount;
  const loading = tab === 0 ? listLoading : treeLoading;

  useEffect(() => {
    if (tab === 1) setExpanded(new Set(pagedTree.map((item) => item.id)));
  }, [tab, pagedTree]);

  const changeTab = (_event: unknown, value: number) => {
    setTab(value);
    setPage(0);
  };
  const sortRows = (column: string) => {
    toggle(column);
    setPage(0);
  };
  const toggleBranch = (id: number) => {
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const actionButtons = (reseller: ResellerRow) => (
    <Stack direction="row" spacing={0.2} justifyContent="flex-end">
      <Tooltip title="ویرایش">
        <IconButton size="small" onClick={() => setForm({ ...reseller })}>
          <EditIcon fontSize="small" />
        </IconButton>
      </Tooltip>
      <Tooltip title="افزایش ظرفیت کاربران">
        <IconButton
          size="small"
          color="primary"
          onClick={() => {
            setBumpAmount(100);
            setBumpRow(reseller);
          }}
        >
          <AddIcon fontSize="small" />
        </IconButton>
      </Tooltip>
      {reseller.enforcement_state === "enforced" ? (
        <Tooltip title="بازگردانی">
          <IconButton
            size="small"
            color="success"
            disabled={restore.isPending}
            onClick={() => restore.mutate(reseller.id)}
          >
            <RestartAltIcon fontSize="small" />
          </IconButton>
        </Tooltip>
      ) : (
        <Tooltip title="مسدودسازی">
          <IconButton
            size="small"
            color="error"
            disabled={enforce.isPending}
            onClick={() => {
              if (confirm("مسدودسازی این نماینده؟ (در حالت آزمایشی فقط ثبت می‌شود)")) {
                enforce.mutate(reseller.id);
              }
            }}
          >
            <BlockIcon fontSize="small" />
          </IconButton>
        </Tooltip>
      )}
    </Stack>
  );

  const canAddSwitch = (reseller: ResellerRow) => (
    <Tooltip
      title={reseller.can_add_admin
        ? "اجازهٔ ساخت زیرمجموعه دارد"
        : "اجازهٔ ساخت زیرمجموعه ندارد"}
    >
      <Stack direction="row" alignItems="center" spacing={0.5}>
        <Switch
          size="small"
          checked={!!reseller.can_add_admin}
          disabled={canAdd.isPending}
          onChange={(event) =>
            canAdd.mutate({ id: reseller.id, enabled: event.target.checked })}
        />
        <Typography variant="caption" color="text.secondary">
          {reseller.can_add_admin ? "فعال" : "غیرفعال"}
        </Typography>
      </Stack>
    </Tooltip>
  );

  return (
    <Box>
      <Stack
        direction={{ xs: "column", sm: "row" }}
        alignItems={{ xs: "stretch", sm: "flex-end" }}
        justifyContent="space-between"
        spacing={2}
        sx={{ mb: 2.5 }}
      >
        <Box>
          <Typography variant="h5">نمایندگان</Typography>
          <Typography variant="body2" color="text.secondary" sx={{ mt: 0.45 }}>
            {fmtNum(currentCount)} نماینده در نمای فعلی
          </Typography>
        </Box>
        <Stack direction={{ xs: "column", sm: "row" }} spacing={1.2}>
          <TextField
            size="small"
            placeholder="جستجوی نام یا شناسه نماینده..."
            value={q}
            onChange={(event) => {
              setQ(event.target.value);
              setPage(0);
            }}
            InputProps={{
              startAdornment: (
                <InputAdornment position="start">
                  <SearchIcon fontSize="small" />
                </InputAdornment>
              ),
            }}
            sx={{ width: { xs: "100%", sm: 280 } }}
          />
          <TextField
            select
            size="small"
            value={panelId}
            SelectProps={{
              displayEmpty: true,
              renderValue: (value) => {
                if (!value) return "همهٔ پنل‌ها";
                return panels.find((panel: any) => String(panel.id) === String(value))?.key || value;
              },
            }}
            onChange={(event) => {
              setPanelId(event.target.value);
              setPage(0);
            }}
            sx={{ minWidth: { xs: "100%", sm: 155 } }}
          >
            <MenuItem value="">همهٔ پنل‌ها</MenuItem>
            {panels.map((panel: any) => (
              <MenuItem key={panel.id} value={panel.id}>{panel.key}</MenuItem>
            ))}
          </TextField>
        </Stack>
      </Stack>

      <Stack
        direction={{ xs: "column", md: "row" }}
        justifyContent="space-between"
        alignItems={{ xs: "stretch", md: "center" }}
        spacing={1.5}
        sx={{ mb: 2 }}
      >
        <Box
          sx={{
            p: 0.45,
            border: 1,
            borderColor: "divider",
            borderRadius: 2.5,
            bgcolor: alpha(theme.palette.background.paper, 0.46),
            alignSelf: { xs: "stretch", md: "flex-start" },
          }}
        >
          <Tabs
            value={tab}
            onChange={changeTab}
            variant="fullWidth"
            sx={{
              minHeight: 38,
              "& .MuiTabs-indicator": { display: "none" },
              "& .MuiTab-root": {
                minHeight: 38,
                px: 2,
                py: 0.7,
                borderRadius: 2,
                color: "text.secondary",
              },
              "& .Mui-selected": {
                color: "text.primary !important",
                bgcolor: alpha(theme.palette.primary.main, theme.palette.mode === "dark" ? 0.17 : 0.1),
                boxShadow: `0 2px 8px ${alpha(theme.palette.primary.main, 0.1)}`,
              },
            }}
          >
            <Tab icon={<FormatListBulletedIcon />} iconPosition="start" label="فهرست اصلی" />
            <Tab icon={<AccountTreeIcon />} iconPosition="start" label="درخت زیرمجموعه‌ها" />
          </Tabs>
        </Box>

        <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap>
          {tab === 0 ? (
            <>
              <Chip
                size="small"
                icon={<CheckCircleOutlineIcon />}
                label={`${fmtNum(billableCount)} مشمول فاکتور`}
                color="success"
                variant="outlined"
              />
              {exemptCount > 0 && (
                <Chip
                  size="small"
                  icon={<WarningAmberIcon />}
                  label={`${fmtNum(exemptCount)} معاف`}
                  color="warning"
                  variant="outlined"
                />
              )}
            </>
          ) : (
            <>
              <Typography variant="caption" color="text.secondary">
                {fmtNum(tree.length)} شاخه اصلی · {fmtNum(treeCount - tree.length)} زیرمجموعه
              </Typography>
              {tree.length > 0 && (
                <Button
                  size="small"
                  variant="text"
                  onClick={() => {
                    if (expanded.size) setExpanded(new Set());
                    else setExpanded(new Set(allBranchIds));
                  }}
                >
                  {expanded.size ? "بستن همه" : "باز کردن شاخه‌ها"}
                </Button>
              )}
            </>
          )}
        </Stack>
      </Stack>

      <Card sx={{ overflow: "hidden" }}>
        {loading ? (
          <Stack spacing={1} sx={{ p: 2 }}>
            {[0, 1, 2, 3, 4].map((item) => (
              <Skeleton key={item} variant="rounded" height={66} />
            ))}
          </Stack>
        ) : (
          <>
            {!isMobile && (
            <TableContainer>
              <Table size="small" sx={{ minWidth: 1080 }}>
                <TableHead>
                  <TableRow>
                    <SortTh id="name" label="نماینده" sortKey={key} dir={dir} onSort={sortRows} />
                    <SortTh id="panel_key" label="پنل" sortKey={key} dir={dir} onSort={sortRows} />
                    <SortTh id="effective_price_per_gb" label="قیمت/گیگ" sortKey={key} dir={dir} onSort={sortRows} />
                    <SortTh id="capacity_pct" label="پُری ظرفیت" sortKey={key} dir={dir} onSort={sortRows} />
                    <SortTh id="can_add_admin" label="زیرمجموعه" sortKey={key} dir={dir} onSort={sortRows} />
                    <SortTh id="registered" label="ربات" sortKey={key} dir={dir} onSort={sortRows} />
                    <SortTh id="enforcement_state" label="وضعیت" sortKey={key} dir={dir} onSort={sortRows} />
                    <SortTh id="exclude_from_billing" label="فاکتور" sortKey={key} dir={dir} onSort={sortRows} />
                    <TableCell align="left">عملیات</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {tab === 0
                    ? rows.map((reseller) => (
                      <ResellerTableRow
                        key={reseller.id}
                        reseller={reseller}
                        actions={actionButtons}
                        canAddSwitch={canAddSwitch}
                      />
                    ))
                    : visibleTreeRows.map(({ node: reseller, depth }) => (
                      <ResellerTableRow
                        key={reseller.id}
                        reseller={reseller}
                        depth={depth}
                        tree
                        expanded={expanded.has(reseller.id)}
                        onToggle={() => toggleBranch(reseller.id)}
                        actions={actionButtons}
                        canAddSwitch={canAddSwitch}
                      />
                    ))}
                  {currentCount === 0 && (
                    <TableRow>
                      <TableCell colSpan={9} align="center" sx={{ py: 7, color: "text.secondary" }}>
                        نماینده‌ای با این فیلتر پیدا نشد.
                      </TableCell>
                    </TableRow>
                  )}
                </TableBody>
              </Table>
            </TableContainer>
            )}

            {isMobile && (
            <Stack spacing={1.2} sx={{ p: 1.5 }}>
              {(tab === 0
                ? rows.map((reseller) => ({ reseller, depth: 0 }))
                : visibleTreeRows.map(({ node: reseller, depth }) => ({ reseller, depth }))
              ).map(({ reseller, depth }) => (
                <ResellerMobileCard
                  key={reseller.id}
                  reseller={reseller}
                  depth={depth}
                  tree={tab === 1}
                  expanded={expanded.has(reseller.id)}
                  onToggle={() => toggleBranch(reseller.id)}
                  actions={actionButtons}
                  canAddSwitch={canAddSwitch}
                />
              ))}
              {currentCount === 0 && (
                <Typography align="center" color="text.secondary" variant="body2" sx={{ py: 5 }}>
                  نماینده‌ای با این فیلتر پیدا نشد.
                </Typography>
              )}
            </Stack>
            )}

            {(tab === 0 ? data.length : tree.length) > 0 && (
              <TablePagination
                component="div"
                count={tab === 0 ? data.length : tree.length}
                page={page}
                rowsPerPage={25}
                rowsPerPageOptions={[25]}
                onPageChange={(_event, nextPage) => setPage(nextPage)}
                labelDisplayedRows={({ from, to, count }) =>
                  `${fmtNum(from)}–${fmtNum(to)} از ${fmtNum(count)}`}
              />
            )}
          </>
        )}
      </Card>

      <Dialog open={!!form} onClose={() => setForm(null)} fullWidth maxWidth="xs">
        <DialogTitle>ویرایش نماینده {form?.name}</DialogTitle>
        <DialogContent>
          <Stack spacing={2} sx={{ mt: 1 }}>
            <TextField
              label="قیمت هر گیگ (تومان) — خالی = پیش‌فرض"
              type="number"
              value={form?.price_per_gb ?? ""}
              onChange={(event) => setForm(form ? {
                ...form,
                price_per_gb: event.target.value as any,
              } : null)}
            />
            <TextField
              label="حداقل فروش (تومان) — خالی = پیش‌فرض، ۰ = بدون حداقل"
              type="number"
              value={form?.min_sale_toman ?? ""}
              onChange={(event) => setForm(form ? {
                ...form,
                min_sale_toman: event.target.value as any,
              } : null)}
              helperText="برای کل مجموعهٔ این نماینده (خودش + زیرمجموعه‌ها) اعمال می‌شود"
            />
            <FormControlLabel
              control={
                <Switch
                  checked={!!form?.exclude_from_billing}
                  onChange={(event) => setForm(form ? {
                    ...form,
                    exclude_from_billing: event.target.checked,
                  } : null)}
                />
              }
              label="معاف از صدور فاکتور"
            />
          </Stack>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setForm(null)}>انصراف</Button>
          <Button variant="contained" disabled={save.isPending} onClick={() => save.mutate()}>
            ذخیره
          </Button>
        </DialogActions>
      </Dialog>

      <Dialog open={!!bumpRow} onClose={() => setBumpRow(null)} fullWidth maxWidth="xs">
        {bumpRow && (
          <>
            <DialogTitle>افزایش ظرفیت — {bumpRow.name}</DialogTitle>
            <DialogContent>
              <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
                این مقدار به هر دو سقف «تعداد کاربران» و «کاربران فعال» این نماینده روی پنل اضافه می‌شود.
                {bumpRow.panel_max_users != null && (
                  <> سقف فعلی: {fmtNum(bumpRow.panel_max_users)} (ساخته: {fmtNum(bumpRow.users_count)}).</>
                )}
              </Typography>
              <Stack direction="row" spacing={1} sx={{ mb: 2 }}>
                {[50, 100, 200, 500].map((amount) => (
                  <Button
                    key={amount}
                    size="small"
                    variant={bumpAmount === amount ? "contained" : "outlined"}
                    onClick={() => setBumpAmount(amount)}
                  >
                    +{fmtNum(amount)}
                  </Button>
                ))}
              </Stack>
              <TextField
                type="number"
                label="مقدار افزایش"
                fullWidth
                value={bumpAmount}
                onChange={(event) =>
                  setBumpAmount(Math.max(1, Number(event.target.value) || 0))}
              />
            </DialogContent>
            <DialogActions>
              <Button onClick={() => setBumpRow(null)}>انصراف</Button>
              <Button
                variant="contained"
                disabled={bump.isPending || bumpAmount < 1}
                onClick={() => bump.mutate({ id: bumpRow.id, amount: bumpAmount })}
              >
                افزودن +{fmtNum(bumpAmount)}
              </Button>
            </DialogActions>
          </>
        )}
      </Dialog>
      {toastNode}
    </Box>
  );
}

function ResellerIdentity({
  reseller,
  depth = 0,
  tree = false,
  expanded = false,
  onToggle,
}: {
  reseller: ResellerRow | ResellerTreeRow;
  depth?: number;
  tree?: boolean;
  expanded?: boolean;
  onToggle?: () => void;
}) {
  const treeRow = reseller as ResellerTreeRow;
  const hasChildren = tree && (treeRow.children?.length || 0) > 0;

  return (
    <Box
      sx={{
        position: "relative",
        display: "flex",
        alignItems: "center",
        minHeight: 43,
        ps: tree ? depth * 3.1 : 0,
      }}
    >
      {tree && depth > 0 && (
        <>
          <Box
            sx={{
              position: "absolute",
              insetInlineStart: (depth - 1) * 24 + 10,
              top: -15,
              bottom: "50%",
              width: 18,
              borderInlineStart: "1px solid",
              borderBottom: "1px solid",
              borderColor: "divider",
              borderEndStartRadius: 8,
            }}
          />
          <Box
            sx={{
              position: "absolute",
              insetInlineStart: (depth - 1) * 24 + 10,
              top: "50%",
              bottom: -16,
              borderInlineStart: "1px solid",
              borderColor: "divider",
            }}
          />
        </>
      )}
      {tree && (
        <Box sx={{ width: 34, flexShrink: 0, display: "grid", placeItems: "center" }}>
          {hasChildren ? (
            <IconButton
              size="small"
              aria-label={expanded ? "بستن زیرمجموعه‌ها" : "باز کردن زیرمجموعه‌ها"}
              onClick={(event: MouseEvent) => {
                event.stopPropagation();
                onToggle?.();
              }}
              sx={{
                width: 28,
                height: 28,
                bgcolor: (theme) => alpha(theme.palette.primary.main, 0.09),
              }}
            >
              {expanded
                ? <KeyboardArrowDownIcon fontSize="small" />
                : <KeyboardArrowLeftIcon fontSize="small" />}
            </IconButton>
          ) : (
            <Box
              sx={{
                width: 7,
                height: 7,
                borderRadius: "50%",
                bgcolor: depth === 0 ? "primary.main" : "text.disabled",
              }}
            />
          )}
        </Box>
      )}
      <Box sx={{ minWidth: 0 }}>
        <Stack direction="row" alignItems="center" spacing={0.8}>
          <Typography
            variant="body2"
            noWrap
            sx={{ fontWeight: depth === 0 ? 800 : 600, maxWidth: 220 }}
          >
            {reseller.name || "بدون نام"}
          </Typography>
          {treeRow.cycle_detected && (
            <Tooltip title="ساختار والد/فرزند این شاخه ناسالم است">
              <WarningAmberIcon color="warning" sx={{ fontSize: 17 }} />
            </Tooltip>
          )}
        </Stack>
        <Typography variant="caption" color="text.secondary" noWrap>
          {hasChildren
            ? `${fmtNum(treeRow.descendant_count)} زیرمجموعه`
            : depth > 0
              ? `زیرمجموعه سطح ${fmtNum(depth)}`
              : "نماینده اصلی"}
        </Typography>
      </Box>
    </Box>
  );
}

function ResellerTableRow({
  reseller,
  depth = 0,
  tree = false,
  expanded,
  onToggle,
  actions,
  canAddSwitch,
}: {
  reseller: ResellerRow | ResellerTreeRow;
  depth?: number;
  tree?: boolean;
  expanded?: boolean;
  onToggle?: () => void;
  actions: (reseller: ResellerRow) => ReactNode;
  canAddSwitch: (reseller: ResellerRow) => ReactNode;
}) {
  return (
    <TableRow
      hover
      sx={{
        bgcolor: (theme) => tree && depth === 0
          ? alpha(theme.palette.primary.main, theme.palette.mode === "dark" ? 0.08 : 0.035)
          : tree && depth > 0
            ? alpha(theme.palette.background.paper, 0.22)
            : undefined,
        "& td": { py: 1.05 },
      }}
    >
      <TableCell sx={{ minWidth: 260 }}>
        <ResellerIdentity
          reseller={reseller}
          depth={depth}
          tree={tree}
          expanded={expanded}
          onToggle={onToggle}
        />
      </TableCell>
      <TableCell>
        <Chip size="small" label={reseller.panel_key} variant="outlined" />
      </TableCell>
      <TableCell>
        <Typography variant="body2" sx={{ fontWeight: 750, whiteSpace: "nowrap" }}>
          {fmtNum(reseller.effective_price_per_gb)}
          <Typography component="span" variant="caption" color="text.secondary"> تومان</Typography>
        </Typography>
        {!reseller.price_per_gb && (
          <Typography variant="caption" color="text.secondary">پیش‌فرض</Typography>
        )}
      </TableCell>
      <TableCell>
        <CapacityBar used={reseller.users_count} max={reseller.panel_max_users} />
      </TableCell>
      <TableCell>{canAddSwitch(reseller)}</TableCell>
      <TableCell><ConnectionStatus connected={reseller.registered} /></TableCell>
      <TableCell><EnforcementStatus state={reseller.enforcement_state} /></TableCell>
      <TableCell>
        {reseller.exclude_from_billing
          ? <Chip size="small" color="warning" variant="outlined" label="معاف" />
          : <Chip size="small" color="success" variant="outlined" label="محاسبه می‌شود" />}
      </TableCell>
      <TableCell align="left">{actions(reseller)}</TableCell>
    </TableRow>
  );
}

function ResellerMobileCard({
  reseller,
  depth,
  tree,
  expanded,
  onToggle,
  actions,
  canAddSwitch,
}: {
  reseller: ResellerRow | ResellerTreeRow;
  depth: number;
  tree: boolean;
  expanded: boolean;
  onToggle: () => void;
  actions: (reseller: ResellerRow) => ReactNode;
  canAddSwitch: (reseller: ResellerRow) => ReactNode;
}) {
  return (
    <Box
      sx={{
        p: 1.5,
        ms: tree ? Math.min(depth * 1.5, 4) : 0,
        borderRadius: 3,
        border: "1px solid",
        borderColor: "divider",
        bgcolor: (theme) => alpha(theme.palette.background.paper, 0.48),
        borderInlineStartWidth: tree && depth > 0 ? 3 : 1,
        borderInlineStartColor: tree && depth > 0 ? "primary.main" : "divider",
      }}
    >
      <ResellerIdentity
        reseller={reseller}
        depth={depth}
        tree={tree}
        expanded={expanded}
        onToggle={onToggle}
      />
      <Stack direction="row" spacing={0.8} flexWrap="wrap" useFlexGap sx={{ mt: 1.2 }}>
        <Chip size="small" label={reseller.panel_key} variant="outlined" />
        <ConnectionStatus connected={reseller.registered} />
        <EnforcementStatus state={reseller.enforcement_state} />
        {reseller.exclude_from_billing && (
          <Chip size="small" color="warning" variant="outlined" label="معاف از فاکتور" />
        )}
      </Stack>
      <Box
        sx={{
          mt: 1.5,
          display: "grid",
          gridTemplateColumns: "1fr 1fr",
          gap: 1.5,
          alignItems: "center",
        }}
      >
        <Box>
          <Typography variant="caption" color="text.secondary">قیمت هر گیگ</Typography>
          <Typography variant="body2" sx={{ fontWeight: 750 }}>
            {fmtNum(reseller.effective_price_per_gb)} تومان
          </Typography>
        </Box>
        <CapacityBar used={reseller.users_count} max={reseller.panel_max_users} />
      </Box>
      <Stack
        direction="row"
        alignItems="center"
        justifyContent="space-between"
        sx={{ mt: 1.2, pt: 1.1, borderTop: 1, borderColor: "divider" }}
      >
        {canAddSwitch(reseller)}
        {actions(reseller)}
      </Stack>
    </Box>
  );
}
