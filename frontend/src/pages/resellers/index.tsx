import { useState } from "react";
import {
  Box,
  Button,
  Card,
  Chip,
  InputAdornment,
  MenuItem,
  Skeleton,
  Stack,
  TablePagination,
  TextField,
  Typography,
  useMediaQuery,
} from "@mui/material";
import { useTheme } from "@mui/material/styles";
import AccountTreeIcon from "@mui/icons-material/esm/AccountTree";
import CheckCircleOutlineIcon from "@mui/icons-material/esm/CheckCircleOutline";
import PersonOffIcon from "@mui/icons-material/esm/PersonOff";
import FormatListBulletedIcon from "@mui/icons-material/esm/FormatListBulleted";
import SearchIcon from "@mui/icons-material/esm/Search";
import WarningAmberIcon from "@mui/icons-material/esm/WarningAmber";
import { useQuery } from "@tanstack/react-query";
import {
  bumpResellerLimits,
  enforceReseller,
  getResellerTree,
  listPanels,
  listResellers,
  ResellerRow,
  restoreReseller,
  setResellerCanAddAdmin,
  updateReseller,
} from "../../api/client";
import SegmentedTabs from "../../components/SegmentedTabs";
import { useSort } from "../../components/sortable";
import { useToast } from "../../components/Toast";
import { useDialogState } from "../../hooks/useDialogState";
import { useToastMutation } from "../../hooks/useToastMutation";
import { fmtNum } from "../../format";
import AbsentResellers from "./AbsentResellers";
import BumpLimitsDialog from "./BumpLimitsDialog";
import EditResellerDialog from "./EditResellerDialog";
import { CanAddSwitch, ResellerActions, ResellerActionsMobile } from "./ResellerActions";
import ResellerMobileCard from "./ResellerMobileCard";
import ResellerTable from "./ResellerTable";
import { countTree, useResellerTree } from "./useResellerTree";

const INVALIDATE = ["resellers", "reseller-tree"];

export default function Resellers() {
  const theme = useTheme();
  const isMobile = useMediaQuery(theme.breakpoints.down("md"));
  const { node: toastNode, show } = useToast();
  const [tab, setTab] = useState(0);
  const [panelId, setPanelId] = useState("");
  const [q, setQ] = useState("");
  const [page, setPage] = useState(0);
  const editDlg = useDialogState<ResellerRow>();
  const bumpDlg = useDialogState<ResellerRow>();
  const [bumpAmount, setBumpAmount] = useState(100);
  const form = editDlg.data;

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

  const save = useToastMutation({
    show,
    mutationFn: () => {
      if (!form) throw new Error("نماینده‌ای انتخاب نشده است");
      return updateReseller(form.id, {
        price_per_gb: form.price_per_gb ? Number(form.price_per_gb) : null,
        min_sale_toman:
          form.min_sale_toman === ("" as any) || form.min_sale_toman == null
            ? null
            : Number(form.min_sale_toman),
        exclude_from_billing: form.exclude_from_billing,
        storefront_enabled: form.storefront_enabled,
        storefront_monthly_fee_toman:
          form.storefront_monthly_fee_toman === ("" as any) || form.storefront_monthly_fee_toman == null
            ? null
            : Number(form.storefront_monthly_fee_toman),
      });
    },
    success: "ذخیره شد",
    onSuccess: () => editDlg.close(),
    invalidate: INVALIDATE,
  });
  const enforce = useToastMutation({
    show,
    mutationFn: (id: number) => enforceReseller(id),
    success: (result) => [
      result.dry_run
        ? `حالت آزمایشی: ${result.affected_users} کاربر`
        : result.queued
          ? "مسدودسازی در صف ثبت شد"
          : "این نماینده از قبل مسدود است",
      result.dry_run || result.queued ? "info" : "success",
    ],
    invalidate: INVALIDATE,
  });
  const restore = useToastMutation({
    show,
    mutationFn: (id: number) => restoreReseller(id),
    success: (result) => [
      result.queued
        ? "آزادسازی در صف ثبت شد"
        : result.status === "not_enforced"
          ? "این نماینده مسدود نیست"
          : `بازگردانی: ${result.status}`,
      result.queued ? "info" : "success",
    ],
    invalidate: INVALIDATE,
  });
  const bump = useToastMutation({
    show,
    mutationFn: ({ id, amount }: { id: number; amount: number }) =>
      bumpResellerLimits(id, amount),
    success: (result) => `ظرفیت افزایش یافت → سقف کاربران: ${result.max_users}`,
    onSuccess: () => bumpDlg.close(),
    invalidate: INVALIDATE,
  });
  const canAdd = useToastMutation({
    show,
    mutationFn: ({ id, enabled }: { id: number; enabled: boolean }) =>
      setResellerCanAddAdmin(id, enabled),
    success: (result) =>
      result.can_add_admin ? "ساخت زیرمجموعه فعال شد" : "ساخت زیرمجموعه غیرفعال شد",
    invalidate: INVALIDATE,
  });

  const { sorted, key, dir, toggle } = useSort(data, "name", "asc");
  const rows = sorted.slice(page * 25, page * 25 + 25);
  const { expanded, setExpanded, toggleBranch, visibleTreeRows, allBranchIds } =
    useResellerTree(tree, key, dir, page);
  const billableCount = data.filter((item) => !item.exclude_from_billing).length;
  const exemptCount = data.length - billableCount;
  const treeCount = countTree(tree);
  const currentCount = tab === 0 ? data.length : treeCount;
  const loading = tab === 0 ? listLoading : treeLoading;

  // The tree starts COLLAPSED so its default height/scroll matches the main list (a compact page
  // of root rows); branches expand on demand (chevrons) or all at once via «باز کردن شاخه‌ها».

  const changeTab = (_event: unknown, value: number) => {
    setTab(value);
    setPage(0);
  };
  const sortRows = (column: string) => { toggle(column); setPage(0); };

  const actionArgs = (reseller: ResellerRow) => ({
    reseller,
    onEdit: (r: ResellerRow) => editDlg.openWith({ ...r }),
    onBump: (r: ResellerRow) => {
      setBumpAmount(100);
      bumpDlg.openWith(r);
    },
    enforce,
    restore,
  });
  const actionButtons = (reseller: ResellerRow) => <ResellerActions {...actionArgs(reseller)} />;
  const actionButtonsMobile = (reseller: ResellerRow) => <ResellerActionsMobile {...actionArgs(reseller)} />;

  const canAddSwitch = (reseller: ResellerRow) => (
    <CanAddSwitch reseller={reseller} canAdd={canAdd} />
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
            placeholder="جستجوی نام یا شناسه..."
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
        <SegmentedTabs
          value={tab}
          onChange={(v) => changeTab(null as any, v)}
          tabs={[
            { label: "فهرست اصلی", icon: <FormatListBulletedIcon fontSize="small" /> },
            { label: "درخت زیرمجموعه‌ها", icon: <AccountTreeIcon fontSize="small" /> },
            { label: "نماینده‌های غایب", icon: <PersonOffIcon fontSize="small" /> },
          ]}
        />

        <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap>
          {tab === 2 ? (
            <Typography variant="caption" color="text.secondary">
              ادمین‌هایی که از پنل حذف شده‌اند ولی ردیفشان در سامانه مانده است.
            </Typography>
          ) : tab === 0 ? (
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

      {tab === 2 ? (
        <AbsentResellers panelId={panelId} />
      ) : (
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
              <ResellerTable
                tab={tab}
                rows={rows}
                visibleTreeRows={visibleTreeRows}
                expanded={expanded}
                onToggleBranch={toggleBranch}
                sortKey={key}
                dir={dir}
                onSort={sortRows}
                currentCount={currentCount}
                actions={actionButtons}
                canAddSwitch={canAddSwitch}
              />
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
                  actions={actionButtonsMobile}
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
      )}

      <EditResellerDialog
        form={form}
        onChange={editDlg.setData}
        onClose={editDlg.close}
        onSave={() => save.mutate()}
        saving={save.isPending}
      />

      <BumpLimitsDialog
        row={bumpDlg.data}
        amount={bumpAmount}
        onAmountChange={setBumpAmount}
        onClose={bumpDlg.close}
        onSubmit={(id, amount) => bump.mutate({ id, amount })}
        pending={bump.isPending}
      />

      {toastNode}
    </Box>
  );
}
