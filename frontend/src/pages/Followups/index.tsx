import { useEffect, useMemo, useState } from "react";
import {
  Box, Button, Card, Checkbox, Chip, InputAdornment, MenuItem, Select, Stack, Table, TableBody,
  TableCell, TableContainer, TableHead, TableRow, TextField, Tooltip, Typography,
} from "@mui/material";
import { alpha } from "@mui/material/styles";
import DownloadIcon from "@mui/icons-material/esm/Download";
import SearchIcon from "@mui/icons-material/esm/Search";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  clearCrmSnooze, getCrmBoardPaged, getCrmSummary, listPanels, logCrmFollowup,
  logCrmFollowupsBulk, type CrmBoardRow,
} from "../../api/client";
import { DataState } from "../../components/DataState";
import SegmentedTabs from "../../components/SegmentedTabs";
import StatCard from "../../components/StatCard";
import { TablePager } from "../../components/TablePager";
import { useToast } from "../../components/Toast";
import { useToastMutation } from "../../hooks/useToastMutation";
import { downloadCsv } from "../../csv";
import { fmtDate, fmtDateTime, fmtGb, fmtNum, fmtToman } from "../../format";
import { TABLE_SCROLL_BOUND } from "../../themeTokens";
import FollowupDialog, { type FollowupDraft } from "./FollowupDialog";
import ResellerDrawer from "./ResellerDrawer";
import SegmentMessage from "./SegmentMessage";
import { SEGMENTS, SegmentChip } from "./segments";

const VIEWS = [
  { key: "due", label: "سررسید پیگیری" },
  { key: "all", label: "همه" },
  { key: "snoozed", label: "تعویق‌شده و بی‌خیال" },
];

const SORTS = [
  { key: "value", label: "ارزش ماهانه" },
  { key: "days", label: "روزهای بی‌فروش" },
  { key: "debt", label: "بدهی" },
  { key: "mtd", label: "فروش این ماه" },
  { key: "touch", label: "آخرین پیگیری" },
  { key: "name", label: "نام" },
];

/** The row's own last-6-months trend, drawn as plain divs — 100 canvases in one table would
 * cost far more than the signal is worth. The drawer has the real chart. */
function Sparkline({ values }: { values: number[] }) {
  const max = Math.max(...values, 1);
  return (
    <Stack direction="row" spacing={0.4} alignItems="flex-end" sx={{ height: 24 }}>
      {values.map((v, i) => (
        <Box key={i} sx={{
          width: 5,
          height: `${Math.max(2, (v / max) * 24)}px`,
          borderRadius: 0.5,   // pill (clamped by the 5px bar width)
          bgcolor: (t) => alpha(t.palette.primary.main, v > 0 ? 0.75 : 0.2),
        }} />
      ))}
      {values.length === 0 && (
        <Typography variant="body2" color="text.secondary">—</Typography>
      )}
    </Stack>
  );
}

export default function Followups() {
  const { node: toastNode, show } = useToast();
  const qc = useQueryClient();
  const [view, setView] = useState(0);
  const [segment, setSegment] = useState("");
  const [panelId, setPanelId] = useState<number | "">("");
  const [q, setQ] = useState("");
  const [sort, setSort] = useState("value");
  const [page, setPage] = useState(0);
  const [rpp, setRpp] = useState(25);
  const [selected, setSelected] = useState<number[]>([]);
  const [draft, setDraft] = useState<FollowupDraft | null>(null);
  const [drawerId, setDrawerId] = useState<number | null>(null);
  useEffect(() => { setPage(0); setSelected([]); }, [view, segment, panelId, q, sort]);

  const viewKey = VIEWS[view].key;
  const { data: panels } = useQuery({ queryKey: ["panels"], queryFn: () => listPanels() });
  const { data: summary } = useQuery({
    queryKey: ["crm-summary"],
    queryFn: ({ signal }) => getCrmSummary(signal),
  });
  const { data: pageData, isLoading, isError, refetch } = useQuery({
    queryKey: ["crm-board", viewKey, segment, panelId, q, sort, rpp, page],
    queryFn: ({ signal }) => getCrmBoardPaged({
      view: viewKey, segment: segment || undefined,
      panel_id: panelId === "" ? undefined : panelId,
      q: q || undefined, sort, order: sort === "name" ? "asc" : "desc",
      limit: rpp, offset: page * rpp,
    }, signal),
  });
  const rows: CrmBoardRow[] = pageData?.rows ?? [];
  const total = pageData?.total ?? 0;

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["crm-board"] });
    qc.invalidateQueries({ queryKey: ["crm-summary"] });
    qc.invalidateQueries({ queryKey: ["crm-reseller"] });
  };

  const followup = useToastMutation({
    show,
    mutationFn: (vars: { ids: number[]; body: any }) =>
      vars.ids.length === 1
        ? logCrmFollowup(vars.ids[0], vars.body)
        : logCrmFollowupsBulk({ ...vars.body, reseller_ids: vars.ids }),
    success: (_r, vars) => `پیگیری برای ${fmtNum(vars.ids.length)} نماینده ثبت شد`,
    onSuccess: () => { setDraft(null); setSelected([]); refresh(); },
  });
  const unsnooze = useToastMutation({
    show,
    mutationFn: (id: number) => clearCrmSnooze(id),
    success: "به فهرست کاری برگشت",
    onSuccess: refresh,
  });

  const counts = summary?.counts || {};
  const toggle = (id: number) =>
    setSelected((s) => (s.includes(id) ? s.filter((x) => x !== id) : [...s, id]));
  const allShownSelected = rows.length > 0 && rows.every((r) => selected.includes(r.reseller_id));

  const exportCsv = () => downloadCsv(
    "followups.csv",
    ["نماینده", "UUID", "پنل", "دسته", "ارزش ماهانه (تومان)", "آخرین فروش", "روزهای بی‌فروش",
      "سرویس این ماه", "حجم این ماه", "بدهی (تومان)", "آخرین پیگیری", "تعویق تا"],
    rows.map((r) => [
      r.reseller_name, r.admin_uuid, r.panel_key, r.segment, r.value_at_risk_toman,
      r.last_sale_date || "", r.days_since_last_sale ?? "", r.mtd_services, r.mtd_gb,
      r.outstanding_toman, r.last_touch_at || "", r.snoozed_until || "",
    ]),
  );

  const tiles = useMemo(() => ([
    { label: "نیازمند پیگیری", value: fmtNum(summary?.due ?? 0), color: "#f43f5e",
      sub: `از ${fmtNum(summary?.total ?? 0)} نمایندهٔ سطح‌یک` },
    { label: "خوابیده و ریزش‌کرده", color: "#f59e0b",
      value: fmtNum((counts.dormant || 0) + (counts.churned || 0)),
      sub: "سرویس جدید نمی‌سازند" },
    { label: "هرگز فعال نشده", value: fmtNum(counts.never_active || 0), color: "#a855f7",
      sub: "پنل گرفته‌اند و کاربری نساخته‌اند" },
    { label: "مسدود و بدهکار", color: "#0071e3",
      value: fmtNum((counts.suspended || 0) + (counts.frozen || 0) + (counts.debtor || 0)),
      sub: "پول از دستشان طلب داریم" },
  ]), [summary, counts]);

  return (
    <Box>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
        هر نمایندهٔ سطح‌یک دقیقاً در یک دسته قرار می‌گیرد، بر اساس آخرین سرویسی که فروخته است.
        با انتخاب هر دسته، یک پیام آمادهٔ مخصوص همان دسته نمایش داده می‌شود که می‌توانید کپی
        کنید و در تلگرام بفرستید. بعد از اینکه دستی در تلگرام به کسی پیام دادید، «ثبت پیگیری»
        بزنید تا تا پایان مهلت تعویق دوباره در این فهرست نیاید. آستانه‌ها در «تنظیمات →
        پیگیری نمایندگان» قابل تغییرند.
      </Typography>

      <Box sx={{ display: "grid", gap: 2, mb: 2.5,
        gridTemplateColumns: { xs: "1fr", sm: "1fr 1fr", lg: "repeat(4, 1fr)" } }}>
        {tiles.map((t) => (
          <StatCard key={t.label} label={t.label} value={t.value} sub={t.sub} color={t.color} />
        ))}
      </Box>

      <Stack direction={{ xs: "column", md: "row" }} spacing={1.5} sx={{ mb: 2 }}
        alignItems={{ md: "center" }}>
        <SegmentedTabs value={view} onChange={setView} tabs={VIEWS.map((v) => ({ label: v.label }))} />
        <TextField size="small" value={q} placeholder="جستجوی نماینده…"
          onChange={(e) => setQ(e.target.value)}
          InputProps={{ startAdornment: (
            <InputAdornment position="start"><SearchIcon fontSize="small" /></InputAdornment>
          ) }} />
        <Select size="small" value={panelId} displayEmpty
          onChange={(e) => setPanelId(e.target.value as number | "")}
          renderValue={(v: any) => (v === "" ? "همهٔ پنل‌ها"
            : (panels || []).find((p: any) => p.id === v)?.key || String(v))}
          sx={{ minWidth: 140 }}>
          <MenuItem value="">همهٔ پنل‌ها</MenuItem>
          {(panels || []).map((p: any) => (
            <MenuItem key={p.id} value={p.id}>{p.key}</MenuItem>
          ))}
        </Select>
        <Select size="small" value={sort} onChange={(e) => setSort(e.target.value)}
          sx={{ minWidth: 160 }}>
          {SORTS.map((s) => <MenuItem key={s.key} value={s.key}>مرتب‌سازی: {s.label}</MenuItem>)}
        </Select>
        <Box sx={{ flexGrow: 1 }} />
        <Button size="small" variant="outlined" startIcon={<DownloadIcon />} onClick={exportCsv}
          disabled={rows.length === 0}>CSV</Button>
      </Stack>

      <Stack direction="row" spacing={1} useFlexGap flexWrap="wrap" sx={{ mb: 2 }}>
        <Chip size="small" label={`همه (${fmtNum(summary?.total ?? 0)})`}
          color={segment === "" ? "primary" : "default"}
          variant={segment === "" ? "filled" : "outlined"}
          onClick={() => setSegment("")} sx={{ cursor: "pointer" }} />
        {SEGMENTS.filter((s) => (counts[s.key] || 0) > 0).map((s) => (
          <Tooltip key={s.key} title={s.help}>
            <Chip size="small" label={`${s.label} (${fmtNum(counts[s.key] || 0)})`}
              color={segment === s.key ? "primary" : s.color}
              variant={segment === s.key ? "filled" : s.variant}
              onClick={() => setSegment(segment === s.key ? "" : s.key)}
              sx={{ cursor: "pointer" }} />
          </Tooltip>
        ))}
      </Stack>

      {/* Only with a bucket selected: the whole value of the text is that it is true for
        * everyone on screen, which "همه" (all ten segments at once) can never be. */}
      {segment && <SegmentMessage segment={segment} count={counts[segment] || 0} />}

      {selected.length > 0 && (
        <Stack direction="row" spacing={1.5} alignItems="center" sx={{ mb: 2 }}>
          <Typography variant="body2">{fmtNum(selected.length)} نماینده انتخاب شده</Typography>
          <Button size="small" variant="contained"
            onClick={() => setDraft({ ids: selected, title: `${fmtNum(selected.length)} نماینده` })}>
            ثبت پیگیری گروهی
          </Button>
          <Button size="small" onClick={() => setSelected([])}>لغو انتخاب</Button>
        </Stack>
      )}

      <DataState isLoading={isLoading} isError={isError} onRetry={refetch}>
        <Card>
          <TableContainer sx={{ maxHeight: { xs: "none", sm: TABLE_SCROLL_BOUND } }}>
            <Table size="small" stickyHeader className="resp-table" sx={{ minWidth: { sm: 980 } }}>
              <TableHead>
                <TableRow>
                  <TableCell padding="checkbox">
                    <Checkbox size="small" checked={allShownSelected}
                      indeterminate={!allShownSelected && rows.some((r) => selected.includes(r.reseller_id))}
                      inputProps={{ "aria-label": "انتخاب همهٔ سطرهای این صفحه" }}
                      onChange={() => setSelected(allShownSelected
                        ? selected.filter((id) => !rows.some((r) => r.reseller_id === id))
                        : [...new Set([...selected, ...rows.map((r) => r.reseller_id)])])} />
                  </TableCell>
                  <TableCell>نماینده</TableCell>
                  <TableCell>پنل</TableCell>
                  <TableCell>دسته</TableCell>
                  <TableCell>ارزش ماهانه</TableCell>
                  <TableCell>آخرین فروش</TableCell>
                  <TableCell>این ماه</TableCell>
                  <TableCell>روند ۶ ماه</TableCell>
                  <TableCell>بدهی</TableCell>
                  <TableCell>آخرین پیگیری</TableCell>
                  <TableCell>اقدام</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {rows.map((r) => (
                  <TableRow key={r.reseller_id} hover>
                    <TableCell padding="checkbox">
                      <Checkbox size="small" checked={selected.includes(r.reseller_id)}
                        inputProps={{ "aria-label": `انتخاب ${r.reseller_name}` }}
                        onChange={() => toggle(r.reseller_id)} />
                    </TableCell>
                    <TableCell>
                      <Box component="button" onClick={() => setDrawerId(r.reseller_id)}
                        sx={{ background: "none", border: 0, p: 0, font: "inherit", cursor: "pointer",
                          color: "primary.main", textAlign: "start" }}>
                        {r.reseller_name || r.admin_uuid}
                      </Box>
                      {r.sub_resellers > 0 && (
                        <Typography variant="body2" color="text.secondary">
                          {fmtNum(r.sub_resellers)} زیرمجموعه
                        </Typography>
                      )}
                    </TableCell>
                    <TableCell>{r.panel_key}</TableCell>
                    <TableCell><SegmentChip segment={r.segment} /></TableCell>
                    <TableCell>{fmtToman(r.value_at_risk_toman)}</TableCell>
                    <TableCell>
                      {r.last_sale_date ? (
                        <>
                          {fmtDate(r.last_sale_date)}
                          <Typography variant="body2" color="text.secondary">
                            {fmtNum(r.days_since_last_sale || 0)} روز پیش
                          </Typography>
                        </>
                      ) : "هرگز"}
                    </TableCell>
                    <TableCell>
                      {fmtNum(r.mtd_services)} سرویس
                      <Typography variant="body2" color="text.secondary">{fmtGb(r.mtd_gb)}</Typography>
                    </TableCell>
                    <TableCell><Sparkline values={r.trend_gb} /></TableCell>
                    <TableCell>
                      {r.outstanding_toman > 0 ? fmtToman(r.outstanding_toman) : "—"}
                    </TableCell>
                    <TableCell>
                      {r.last_touch_at ? (
                        <>
                          {fmtDateTime(r.last_touch_at)}
                          {r.snoozed_until && (
                            <Typography variant="body2" color="text.secondary">
                              تا {fmtDate(r.snoozed_until)}
                            </Typography>
                          )}
                          {r.muted && (
                            <Typography variant="body2" color="text.secondary">بی‌خیال</Typography>
                          )}
                        </>
                      ) : "—"}
                    </TableCell>
                    <TableCell>
                      <Stack direction="row" spacing={0.5}>
                        <Button size="small" variant="outlined" disabled={followup.isPending}
                          onClick={() => setDraft({
                            ids: [r.reseller_id], title: r.reseller_name || r.admin_uuid,
                            pinnedNote: r.note,
                          })}>
                          پیگیری
                        </Button>
                        {!r.due && (
                          <Button size="small" disabled={unsnooze.isPending}
                            onClick={() => unsnooze.mutate(r.reseller_id)}>برگرداندن</Button>
                        )}
                      </Stack>
                    </TableCell>
                  </TableRow>
                ))}
                {rows.length === 0 && (
                  <TableRow><TableCell colSpan={11} align="center"
                    sx={{ py: 4, color: "text.secondary" }}>
                    {viewKey === "due"
                      ? "هیچ نماینده‌ای در این فیلتر نیاز به پیگیری ندارد — همه را رسیده‌اید."
                      : "با این فیلترها نماینده‌ای پیدا نشد."}
                  </TableCell></TableRow>
                )}
              </TableBody>
            </Table>
          </TableContainer>
          <TablePager count={total} page={page} rpp={rpp} onPage={setPage}
            onRpp={(v) => { setRpp(v); setPage(0); }} />
        </Card>
      </DataState>

      <FollowupDialog
        draft={draft}
        defaultSnoozeDays={summary?.snooze_default_days ?? 15}
        busy={followup.isPending}
        onClose={() => setDraft(null)}
        onSubmit={(body) => draft && followup.mutate({ ids: draft.ids, body })}
      />
      <ResellerDrawer
        resellerId={drawerId}
        onClose={() => setDrawerId(null)}
        onFollowup={(id, name, pinnedNote) => setDraft({ ids: [id], title: name, pinnedNote })}
        onClearSnooze={(id) => unsnooze.mutate(id)}
      />
      {toastNode}
    </Box>
  );
}
