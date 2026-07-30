import { ReactNode, useEffect, useMemo, useRef, useState } from "react";
import {
  Alert, Box, Button, Card, Chip, Grid, IconButton, InputAdornment, Skeleton, Stack, Table,
  TableBody, TableCell, TableContainer, TableHead, TableRow, TableSortLabel, TextField, Tooltip,
  Typography,
} from "@mui/material";
import { alpha, useTheme } from "@mui/material/styles";
import ArrowDownwardIcon from "@mui/icons-material/esm/ArrowDownward";
import ArrowUpwardIcon from "@mui/icons-material/esm/ArrowUpward";
import AutorenewIcon from "@mui/icons-material/esm/Autorenew";
import DownloadIcon from "@mui/icons-material/esm/Download";
import GroupIcon from "@mui/icons-material/esm/Group";
import HourglassEmptyIcon from "@mui/icons-material/esm/HourglassEmpty";
import RefreshIcon from "@mui/icons-material/esm/Refresh";
import SearchIcon from "@mui/icons-material/esm/Search";
import StorefrontIcon from "@mui/icons-material/esm/Storefront";
import TrendingUpIcon from "@mui/icons-material/esm/TrendingUp";
import VpnKeyIcon from "@mui/icons-material/esm/VpnKey";
import WalletIcon from "@mui/icons-material/esm/AccountBalanceWallet";
import WarningAmberIcon from "@mui/icons-material/esm/WarningAmber";
import { useQuery } from "@tanstack/react-query";
import { getStorefrontAnalytics, StorefrontAnalytics as Analytics, StorefrontShopRow } from "../api/client";
import StatCard, { currentPeriod } from "../components/StatCard";
import PeriodPicker from "../components/PeriodPicker";
import SegmentedTabs from "../components/SegmentedTabs";
import EChart from "../components/EChart";
import { chartTooltip } from "../components/chartTooltip";
import { TablePager } from "../components/TablePager";
import { CountUp, Reveal } from "../components/motion";
import { SectionCard, EmptyState } from "../portal/ui";
import { CHART_FONT, TABLE_SCROLL_BOUND } from "../themeTokens";
import { downloadCsv } from "../csv";
import { fmtDateTime, fmtGb, fmtNum, fmtToman } from "../format";

const FONT = CHART_FONT;
// Canonical accents (docs/design-tokens.json) — one hue per meaning, reused by chart + tile.
const C = {
  blue: "#0071e3",
  green: "#30d158",
  teal: "#14b8a6",
  amber: "#ff9500",
  red: "#f43f5e",
  violet: "#bf5af2",
  sky: "#32ade6",
  pink: "#ec4899",
  slate: "#64748b",
  indigo: "#8b5cf6",
};
const RANK_COLORS = [C.blue, C.green, C.amber, C.sky, C.violet];

const METHOD_FA: Record<string, string> = {
  card: "کارت‌به‌کارت",
  usdt: "تتر (USDT)",
  ton: "تون (TON)",
  manual: "دستی (توسط فروشنده)",
  unknown: "نامشخص",
};

const pct = (v: number) => `${v.toLocaleString("fa-IR", { maximumFractionDigits: 1 })}٪`;
const share = (part: number, whole: number) => (whole > 0 ? (part / whole) * 100 : 0);

/** Compact Toman for a chart axis — the full amount stays in the tooltip. */
const axisToman = (v: number) =>
  v >= 1_000_000 ? `${fmtNum(Math.round(v / 1_000_000))}م`
    : v >= 1_000 ? `${fmtNum(Math.round(v / 1_000))}هزار`
      : fmtNum(v);

/** Days between a timestamp and now — «۱۲ روز پیش» is the signal, not the exact clock. */
function daysAgo(iso: string | null): number | null {
  if (!iso) return null;
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return null;
  return Math.floor((Date.now() - then) / 86_400_000);
}

function Delta({ current, previous, label }: { current: number; previous: number; label: string }) {
  const theme = useTheme();
  if (!previous) {
    return (
      <Typography component="span" variant="caption" color="text.secondary" sx={{ fontWeight: 650 }}>
        {current ? `بدون ${label} برای مقایسه` : `${label}: بدون فروش`}
      </Typography>
    );
  }
  const change = ((current - previous) / previous) * 100;
  const up = change >= 0;
  return (
    <Stack direction="row" spacing={0.5} alignItems="center"
      sx={{ color: up ? theme.palette.success.main : theme.palette.error.main }}>
      {up ? <ArrowUpwardIcon sx={{ fontSize: 15 }} /> : <ArrowDownwardIcon sx={{ fontSize: 15 }} />}
      <Typography component="span" variant="caption" sx={{ color: "inherit", fontWeight: 650 }}>
        {pct(Math.abs(change))} نسبت به {label}
      </Typography>
    </Stack>
  );
}

function Hint({ children, color }: { children: ReactNode; color?: string }) {
  return (
    <Typography component="span" variant="caption"
      sx={{ color: color || "text.secondary", fontWeight: 650 }}>
      {children}
    </Typography>
  );
}

/** A dense label/value tile — the workhorse of the detail grids. */
function Metric({
  label, value, hint, color = C.blue,
}: { label: string; value: ReactNode; hint?: ReactNode; color?: string }) {
  return (
    <Box sx={{
      p: 1.6, borderRadius: 2.5, height: "100%",
      bgcolor: (t) => alpha(color, t.palette.mode === "dark" ? 0.1 : 0.07),
      border: (t) => `1px solid ${alpha(color, t.palette.mode === "dark" ? 0.22 : 0.16)}`,
    }}>
      <Typography variant="caption" color="text.secondary" sx={{ fontWeight: 600 }}>{label}</Typography>
      <Typography sx={{ mt: 0.5, fontWeight: 850, fontSize: 19, lineHeight: 1.25 }}>{value}</Typography>
      {hint && <Box sx={{ mt: 0.4 }}><Hint>{hint}</Hint></Box>}
    </Box>
  );
}

/** One row of a ranked bar list (best-seller boards, method breakdowns, …). */
function RankRow({
  index, title, caption, value, max, valueLabel, color,
}: {
  index: number; title: string; caption?: string; value: number; max: number;
  valueLabel: string; color?: string;
}) {
  const theme = useTheme();
  const tone = color || RANK_COLORS[index % RANK_COLORS.length];
  const width = Math.max(share(value, max), 3);
  return (
    <Box>
      <Stack direction="row" justifyContent="space-between" alignItems="flex-end" spacing={2}>
        <Box sx={{ minWidth: 0 }}>
          <Typography variant="body2" noWrap sx={{ fontWeight: 750 }}>{title}</Typography>
          {caption && <Typography variant="caption" color="text.secondary">{caption}</Typography>}
        </Box>
        <Typography variant="body2" sx={{ fontWeight: 800, whiteSpace: "nowrap" }}>{valueLabel}</Typography>
      </Stack>
      <Box sx={{
        mt: 0.9, height: 8, borderRadius: 980, overflow: "hidden", display: "flex",
        justifyContent: "flex-end", bgcolor: alpha(theme.palette.text.secondary, 0.11),
      }}>
        <Box sx={{
          width: `${width}%`, height: "100%", borderRadius: 980,
          background: `linear-gradient(90deg, ${alpha(tone, 0.7)}, ${tone})`,
        }} />
      </Box>
    </Box>
  );
}

/** A donut + legend pair, used for the bot-status and service-state breakdowns. */
function Donut({
  slices, total, centerLabel, ariaLabel,
}: {
  slices: { label: string; value: number; color: string }[];
  total: number; centerLabel: string; ariaLabel: string;
}) {
  const theme = useTheme();
  const tooltip = chartTooltip(theme);
  const visible = slices.filter((s) => s.value > 0);
  const option = useMemo(() => ({
    textStyle: { fontFamily: FONT },
    tooltip: {
      trigger: "item",
      formatter: (p: any) => `${p.name}: ${fmtNum(p.value)} (${pct(share(p.value, total))})`,
      ...tooltip,
    },
    series: [{
      type: "pie",
      radius: ["66%", "84%"],
      center: ["50%", "50%"],
      label: { show: false },
      itemStyle: { borderRadius: 7, borderColor: theme.palette.background.paper, borderWidth: 3 },
      data: visible.map((s) => ({ value: s.value, name: s.label, itemStyle: { color: s.color } })),
    }],
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }), [visible.map((s) => `${s.label}:${s.value}`).join(","), theme]);

  if (!visible.length) return <EmptyState>داده‌ای برای نمایش نیست.</EmptyState>;
  return (
    <Stack direction={{ xs: "column", sm: "row" }} alignItems="center" spacing={2}>
      <Box sx={{ width: 190, height: 190, position: "relative", flexShrink: 0 }}>
        <EChart option={option} height={190} ariaLabel={ariaLabel} />
        <Box sx={{
          position: "absolute", inset: 0, display: "grid", placeContent: "center",
          textAlign: "center", pointerEvents: "none",
        }}>
          <Typography sx={{ fontSize: 27, fontWeight: 850, lineHeight: 1 }}>{fmtNum(total)}</Typography>
          <Typography variant="caption" color="text.secondary" sx={{ mt: 0.6 }}>{centerLabel}</Typography>
        </Box>
      </Box>
      <Box sx={{ width: "100%", display: "grid", gap: 1, gridTemplateColumns: { xs: "repeat(2, minmax(0,1fr))", sm: "1fr" } }}>
        {slices.map((s) => (
          <Stack key={s.label} direction="row" alignItems="center" justifyContent="space-between"
            spacing={1} sx={{ p: 1, borderRadius: 2, bgcolor: (t) => alpha(s.color, t.palette.mode === "dark" ? 0.09 : 0.07) }}>
            <Stack direction="row" alignItems="center" spacing={0.8} sx={{ minWidth: 0 }}>
              <Box sx={{ width: 8, height: 8, borderRadius: "50%", bgcolor: s.color, flexShrink: 0 }} />
              <Typography variant="caption" noWrap>{s.label}</Typography>
            </Stack>
            <Typography variant="body2" sx={{ fontWeight: 850 }}>{fmtNum(s.value)}</Typography>
          </Stack>
        ))}
      </Box>
    </Stack>
  );
}

// ── the per-shop table ──────────────────────────────────────────────────────────────────
type ShopSort = "net" | "orders" | "customers" | "services" | "wallet" | "pending" | "name";

const SHOP_SORTS: Record<ShopSort, (r: StorefrontShopRow) => number | string> = {
  net: (r) => r.net_sales_toman,
  orders: (r) => r.orders,
  customers: (r) => r.customers,
  services: (r) => r.services_active,
  wallet: (r) => r.wallet_liability_toman,
  pending: (r) => r.pending_topups_toman,
  name: (r) => r.reseller_name,
};

function ShopStatus({ row }: { row: StorefrontShopRow }) {
  if (!row.enabled) return <Chip size="small" label="خاموش" />;
  if (row.status === "errored") return <Chip size="small" color="error" label="خطای توکن" />;
  if (row.shop_closed) return <Chip size="small" color="warning" label="بسته" />;
  return <Chip size="small" color="success" variant="outlined" label="فعال" />;
}

function ShopsTable({ rows }: { rows: StorefrontShopRow[] }) {
  const [q, setQ] = useState("");
  const [sort, setSort] = useState<ShopSort>("net");
  const [order, setOrder] = useState<"asc" | "desc">("desc");
  const [page, setPage] = useState(0);
  const [rpp, setRpp] = useState(25);

  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase();
    const list = needle
      ? rows.filter((r) =>
        r.reseller_name.toLowerCase().includes(needle) ||
        (r.bot_username || "").toLowerCase().includes(needle) ||
        r.panel_key.toLowerCase().includes(needle))
      : rows.slice();
    const key = SHOP_SORTS[sort];
    return list.sort((a, b) => {
      const av = key(a), bv = key(b);
      const cmp = typeof av === "string" && typeof bv === "string"
        ? av.localeCompare(bv, "fa")
        : (av as number) - (bv as number);
      return order === "asc" ? cmp : -cmp;
    });
  }, [rows, q, sort, order]);

  useEffect(() => setPage(0), [q, sort, order]);
  const paged = filtered.slice(page * rpp, page * rpp + rpp);

  const head = (id: ShopSort, label: string) => (
    <TableCell sortDirection={sort === id ? order : false}>
      <TableSortLabel active={sort === id} direction={sort === id ? order : "desc"}
        onClick={() => {
          setSort(id);
          setOrder(sort === id && order === "desc" ? "asc" : "desc");
        }}>
        {label}
      </TableSortLabel>
    </TableCell>
  );

  const exportCsv = () => downloadCsv(
    "storefront-shops.csv",
    ["نماینده", "ربات", "پنل", "وضعیت", "مشتری", "مشتری جدید", "سرویس فعال",
      "فروش دوره (تومان)", "سفارش", "فروش امروز (تومان)", "کیف پول (تومان)",
      "شارژ در انتظار", "آخرین فروش"],
    filtered.map((r) => [
      r.reseller_name, r.bot_username ? `@${r.bot_username}` : "", r.panel_key,
      !r.enabled ? "خاموش" : r.status === "errored" ? "خطای توکن" : r.shop_closed ? "بسته" : "فعال",
      r.customers, r.new_customers, r.services_active, r.net_sales_toman, r.orders,
      r.today_net_toman, r.wallet_liability_toman, r.pending_topups_count,
      r.last_sale_at ? fmtDateTime(r.last_sale_at) : "",
    ]),
  );

  return (
    <Card>
      <Stack direction={{ xs: "column", sm: "row" }} spacing={1.5} alignItems={{ sm: "center" }}
        sx={{ p: 1.8, borderBottom: 1, borderColor: "divider" }}>
        <TextField
          size="small" value={q} onChange={(e) => setQ(e.target.value)}
          placeholder="جست‌وجوی نماینده، ربات یا پنل"
          InputProps={{
            startAdornment: <InputAdornment position="start"><SearchIcon fontSize="small" /></InputAdornment>,
          }}
          sx={{ minWidth: { sm: 280 } }}
        />
        <Box sx={{ flexGrow: 1 }} />
        <Typography variant="body2" color="text.secondary">
          {fmtNum(filtered.length)} فروشگاه
        </Typography>
        <Button size="small" startIcon={<DownloadIcon />} onClick={exportCsv}
          disabled={!filtered.length}>خروجی CSV</Button>
      </Stack>
      <TableContainer sx={{ maxHeight: { xs: "none", sm: TABLE_SCROLL_BOUND } }}>
        <Table size="small" stickyHeader className="resp-table" sx={{ minWidth: { sm: 980 } }}>
          <TableHead>
            <TableRow>
              {head("name", "نماینده / ربات")}
              <TableCell>وضعیت</TableCell>
              {head("customers", "مشتری")}
              {head("services", "سرویس فعال")}
              {head("orders", "سفارش دوره")}
              {head("net", "فروش دوره")}
              <TableCell>فروش امروز</TableCell>
              {head("wallet", "کیف پول مشتریان")}
              {head("pending", "شارژ در انتظار")}
              <TableCell>آخرین فروش</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {paged.map((r) => {
              const idle = daysAgo(r.last_sale_at);
              return (
                <TableRow key={r.shop_id} hover>
                  <TableCell>
                    <Typography variant="body2" sx={{ fontWeight: 700 }}>{r.reseller_name}</Typography>
                    <Typography variant="caption" color="text.secondary">
                      {r.bot_username ? `@${r.bot_username}` : "بدون نام کاربری"} · {r.panel_key}
                    </Typography>
                  </TableCell>
                  <TableCell><ShopStatus row={r} /></TableCell>
                  <TableCell>
                    {fmtNum(r.customers)}
                    {r.new_customers > 0 && (
                      <Typography component="span" variant="caption" color="success.main"
                        sx={{ fontWeight: 700 }}> +{fmtNum(r.new_customers)}</Typography>
                    )}
                  </TableCell>
                  <TableCell>
                    {fmtNum(r.services_active)}
                    {r.expiring_3d > 0 && (
                      <Typography component="span" variant="caption" color="warning.main"
                        sx={{ fontWeight: 700 }}> ({fmtNum(r.expiring_3d)} رو به انقضا)</Typography>
                    )}
                  </TableCell>
                  <TableCell>{fmtNum(r.orders)}</TableCell>
                  <TableCell sx={{ fontWeight: 750 }}>{fmtToman(r.net_sales_toman)}</TableCell>
                  <TableCell>{r.today_net_toman ? fmtToman(r.today_net_toman) : "—"}</TableCell>
                  <TableCell>{fmtToman(r.wallet_liability_toman)}</TableCell>
                  <TableCell>
                    {r.pending_topups_count
                      ? <Chip size="small" color="warning" variant="outlined"
                        label={`${fmtNum(r.pending_topups_count)} · ${fmtToman(r.pending_topups_toman)}`} />
                      : "—"}
                  </TableCell>
                  <TableCell>
                    {idle === null ? "هرگز"
                      : idle === 0 ? "امروز"
                        : <Tooltip title={fmtDateTime(r.last_sale_at)}>
                          <span>{fmtNum(idle)} روز پیش</span>
                        </Tooltip>}
                  </TableCell>
                </TableRow>
              );
            })}
            {!filtered.length && (
              <TableRow>
                <TableCell colSpan={10} align="center" sx={{ py: 4, color: "text.secondary" }}>
                  فروشگاهی با این مشخصات پیدا نشد
                </TableCell>
              </TableRow>
            )}
          </TableBody>
        </Table>
      </TableContainer>
      <TablePager count={filtered.length} page={page} rpp={rpp} onPage={setPage}
        onRpp={(v) => { setRpp(v); setPage(0); }} />
    </Card>
  );
}

// ── tabs ────────────────────────────────────────────────────────────────────────────────
function SalesTab({ data }: { data: Analytics }) {
  const theme = useTheme();
  const isDark = theme.palette.mode === "dark";
  const tooltip = chartTooltip(theme);
  const daily = data.daily;
  const empty = !daily.some((d) => d.net_toman || d.orders);

  const dailyOption = useMemo(() => ({
    textStyle: { fontFamily: FONT },
    grid: { left: 6, right: 6, top: 30, bottom: 4, containLabel: true },
    legend: {
      data: ["فروش خالص", "سفارش‌ها"], top: 0, icon: "roundRect", itemWidth: 10, itemHeight: 10,
      textStyle: { color: theme.palette.text.secondary, fontFamily: FONT, fontSize: 12 },
    },
    tooltip: {
      trigger: "axis",
      axisPointer: { type: "shadow" },
      formatter: (params: any) => {
        const list = Array.isArray(params) ? params : [params];
        const row = daily[list[0]?.dataIndex ?? 0];
        return [
          row?.date ?? "",
          `فروش خالص: <b>${fmtToman(row?.net_toman || 0)}</b>`,
          `سفارش‌ها: <b>${fmtNum(row?.orders || 0)}</b>`,
          `شارژ کیف پول: <b>${fmtToman(row?.topups_toman || 0)}</b>`,
        ].join("<br/>");
      },
      ...tooltip,
    },
    xAxis: {
      type: "category",
      data: daily.map((d) => fmtNum(d.day)),
      axisTick: { show: false },
      axisLine: { lineStyle: { color: alpha(theme.palette.text.secondary, 0.25) } },
      axisLabel: {
        color: theme.palette.text.secondary, fontFamily: FONT, fontSize: 11,
        interval: daily.length > 16 ? 1 : 0,
      },
    },
    yAxis: [
      {
        type: "value",
        axisLabel: { color: theme.palette.text.secondary, fontFamily: FONT, fontSize: 11, formatter: axisToman },
        splitLine: { lineStyle: { color: theme.palette.divider } },
      },
      {
        type: "value",
        axisLabel: { color: theme.palette.text.secondary, fontFamily: FONT, fontSize: 11, formatter: fmtNum },
        splitLine: { show: false },
      },
    ],
    series: [
      {
        name: "فروش خالص",
        type: "bar",
        data: daily.map((d) => d.net_toman),
        barMaxWidth: 18,
        itemStyle: {
          borderRadius: [6, 6, 0, 0],
          color: {
            type: "linear", x: 0, y: 0, x2: 0, y2: 1,
            colorStops: [
              { offset: 0, color: C.blue },
              { offset: 1, color: alpha(C.blue, isDark ? 0.35 : 0.45) },
            ],
          },
        },
      },
      {
        name: "سفارش‌ها",
        type: "line",
        yAxisIndex: 1,
        smooth: true,
        symbol: "circle",
        symbolSize: 6,
        data: daily.map((d) => d.orders),
        lineStyle: { width: 2.5, color: C.amber },
        itemStyle: { color: C.amber },
      },
    ],
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }), [daily, isDark, theme]);

  const windows: { label: string; w: Analytics["sales_today"] }[] = [
    { label: "امروز", w: data.sales_today },
    { label: "دیروز", w: data.sales_yesterday },
    { label: "۷ روز اخیر", w: data.sales_7d },
    { label: "۳۰ روز اخیر", w: data.sales_30d },
    { label: "این دوره", w: data.sales_period },
    { label: `دورهٔ ${data.previous_period}`, w: data.sales_previous_period },
  ];
  const period = data.sales_period;
  const maxPlan = Math.max(...data.top_plans.map((p) => p.amount_toman), 1);
  const maxMethod = Math.max(...data.topups.by_method.map((m) => m.amount_toman), 1);

  return (
    <Grid container spacing={2}>
      <Grid item xs={12}>
        <SectionCard title="روند فروش روزانهٔ همهٔ فروشگاه‌ها"
          action={<Chip size="small" label={data.period} sx={{ fontWeight: 700 }} />}>
          <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 1.5 }}>
            ستون‌ها فروش خالص (پس از کسر بازگشت وجه) و خط، تعداد سفارش‌های پرداخت‌شدهٔ هر روز است.
          </Typography>
          {empty ? <EmptyState>در این دوره فروشی ثبت نشده است.</EmptyState>
            : <EChart option={dailyOption} height={320} ariaLabel="روند فروش روزانهٔ فروشگاه‌ها" />}
        </SectionCard>
      </Grid>

      <Grid item xs={12} lg={7}>
        <SectionCard title="مقایسهٔ بازه‌ها">
          <TableContainer>
            <Table size="small" className="resp-table">
              <TableHead>
                <TableRow>
                  <TableCell>بازه</TableCell>
                  <TableCell>فروش خالص</TableCell>
                  <TableCell>سفارش</TableCell>
                  <TableCell>خرید نو</TableCell>
                  <TableCell>تمدید</TableCell>
                  <TableCell>بازگشت وجه</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {windows.map(({ label, w }) => (
                  <TableRow key={label} hover>
                    <TableCell sx={{ fontWeight: 700 }}>{label}</TableCell>
                    <TableCell sx={{ fontWeight: 750 }}>{fmtToman(w.net_toman)}</TableCell>
                    <TableCell>{fmtNum(w.orders)}</TableCell>
                    <TableCell>{fmtNum(w.purchase_count)} · {fmtToman(w.purchase_toman)}</TableCell>
                    <TableCell>{fmtNum(w.renewal_count)} · {fmtToman(w.renewal_toman)}</TableCell>
                    <TableCell>{w.reversals_toman ? fmtToman(w.reversals_toman) : "—"}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableContainer>
          {period.unknown_count > 0 && (
            <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 1.5 }}>
              {fmtNum(period.unknown_count)} تراکنش این دوره به عملیاتِ ثبت‌شده‌ای وصل نیست و در
              تفکیک «خرید نو / تمدید» نیامده، اما در فروش خالص محاسبه شده است.
            </Typography>
          )}
        </SectionCard>
      </Grid>

      <Grid item xs={12} lg={5}>
        <SectionCard title="ترکیب درآمد این دوره">
          <Stack spacing={2}>
            <RankRow index={0} title="خرید سرویس جدید" value={period.purchase_toman}
              max={Math.max(period.gross_toman, 1)} color={C.blue}
              caption={`${fmtNum(period.purchase_count)} سفارش`}
              valueLabel={fmtToman(period.purchase_toman)} />
            <RankRow index={1} title="تمدید سرویس" value={period.renewal_toman}
              max={Math.max(period.gross_toman, 1)} color={C.green}
              caption={`${fmtNum(period.renewal_count)} سفارش`}
              valueLabel={fmtToman(period.renewal_toman)} />
            {period.unknown_toman > 0 && (
              <RankRow index={2} title="سایر تراکنش‌ها" value={period.unknown_toman}
                max={Math.max(period.gross_toman, 1)} color={C.slate}
                caption={`${fmtNum(period.unknown_count)} تراکنش`}
                valueLabel={fmtToman(period.unknown_toman)} />
            )}
          </Stack>
          <Grid container spacing={1.2} sx={{ mt: 1.2 }}>
            <Grid item xs={6}>
              <Metric label="سهم تمدید از درآمد" color={C.green}
                value={pct(share(period.renewal_toman, period.gross_toman))}
                hint="هرچه بالاتر، مشتریان ماندگارترند" />
            </Grid>
            <Grid item xs={6}>
              <Metric label="میانگین ارزش هر سفارش" color={C.blue}
                value={fmtToman(data.customers.avg_order_toman)} />
            </Grid>
          </Grid>
        </SectionCard>
      </Grid>

      <Grid item xs={12} lg={6}>
        <SectionCard title="پرفروش‌ترین پلن‌ها (همهٔ فروشگاه‌ها)">
          <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 1.8 }}>
            پلن‌ها در هر فروشگاه جداگانه تعریف می‌شوند، پس مقایسه بر پایهٔ «حجم × مدت» انجام شده و
            مبلغ، همان چیزی است که در زمان فروش دریافت شده.
          </Typography>
          {data.top_plans.length ? (
            <Stack spacing={2}>
              {data.top_plans.map((p, i) => (
                <RankRow key={`${p.gb}-${p.days}`} index={i}
                  title={`${fmtGb(p.gb)} · ${fmtNum(p.days)} روزه`}
                  caption={`${fmtNum(p.orders)} فروش`}
                  value={p.amount_toman} max={maxPlan} valueLabel={fmtToman(p.amount_toman)} />
              ))}
            </Stack>
          ) : <EmptyState>در این دوره پلنی فروخته نشده است.</EmptyState>}
        </SectionCard>
      </Grid>

      <Grid item xs={12} lg={6}>
        <SectionCard title="روش‌های شارژ کیف پول"
          action={<Chip size="small" label={fmtToman(data.topups.confirmed_toman)} sx={{ fontWeight: 700 }} />}>
          <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 1.8 }}>
            شارژهای تأییدشدهٔ این دوره به تفکیک روش پرداختِ مشتری به فروشنده.
          </Typography>
          {data.topups.by_method.length ? (
            <Stack spacing={2}>
              {data.topups.by_method.map((m, i) => (
                <RankRow key={m.method} index={i} title={METHOD_FA[m.method] || m.method}
                  caption={`${fmtNum(m.count)} فقره`} value={m.amount_toman} max={maxMethod}
                  valueLabel={fmtToman(m.amount_toman)} />
              ))}
            </Stack>
          ) : <EmptyState>در این دوره شارژ تأییدشده‌ای ثبت نشده است.</EmptyState>}
          <Grid container spacing={1.2} sx={{ mt: 1.2 }}>
            <Grid item xs={6}>
              <Metric label="در انتظار تأیید فروشنده" color={C.amber}
                value={fmtToman(data.topups.pending_toman)}
                hint={`${fmtNum(data.topups.pending_count)} درخواست`} />
            </Grid>
            <Grid item xs={6}>
              <Metric label="ردشده در این دوره" color={C.red}
                value={fmtNum(data.topups.rejected_count)} hint="رسیدهای نامعتبر" />
            </Grid>
          </Grid>
        </SectionCard>
      </Grid>
    </Grid>
  );
}

function CustomersTab({ data }: { data: Analytics }) {
  const theme = useTheme();
  const tooltip = chartTooltip(theme);
  const c = data.customers;
  const daily = data.daily;
  const newcomersEmpty = !daily.some((d) => d.new_customers);

  const option = useMemo(() => ({
    textStyle: { fontFamily: FONT },
    grid: { left: 6, right: 6, top: 18, bottom: 4, containLabel: true },
    tooltip: {
      trigger: "axis",
      formatter: (params: any) => {
        const p = Array.isArray(params) ? params[0] : params;
        const row = daily[p.dataIndex];
        return `${row?.date ?? ""}<br/><b>${fmtNum(row?.new_customers || 0)}</b> مشتری جدید`;
      },
      ...tooltip,
    },
    xAxis: {
      type: "category", data: daily.map((d) => fmtNum(d.day)), axisTick: { show: false },
      axisLine: { lineStyle: { color: alpha(theme.palette.text.secondary, 0.25) } },
      axisLabel: {
        color: theme.palette.text.secondary, fontFamily: FONT, fontSize: 11,
        interval: daily.length > 16 ? 1 : 0,
      },
    },
    yAxis: {
      type: "value", minInterval: 1,
      axisLabel: { color: theme.palette.text.secondary, fontFamily: FONT, fontSize: 11, formatter: fmtNum },
      splitLine: { lineStyle: { color: theme.palette.divider } },
    },
    series: [{
      type: "line", smooth: true, symbol: "circle", symbolSize: 6,
      data: daily.map((d) => d.new_customers),
      lineStyle: { width: 2.5, color: C.teal },
      itemStyle: { color: C.teal },
      areaStyle: {
        color: {
          type: "linear", x: 0, y: 0, x2: 0, y2: 1,
          colorStops: [
            { offset: 0, color: alpha(C.teal, 0.35) },
            { offset: 1, color: alpha(C.teal, 0) },
          ],
        },
      },
    }],
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }), [daily, theme]);

  const trialRate = data.trial.rate === null ? null : data.trial.rate * 100;

  return (
    <Grid container spacing={2}>
      <Grid item xs={12} lg={7}>
        <SectionCard title="مشتریان جدید در هر روز">
          {newcomersEmpty ? <EmptyState>در این دوره مشتری جدیدی ثبت نشده است.</EmptyState>
            : <EChart option={option} height={280} ariaLabel="روند جذب مشتری جدید" />}
        </SectionCard>
      </Grid>

      <Grid item xs={12} lg={5}>
        <SectionCard title="قیف تست رایگان به خرید">
          <Stack spacing={2}>
            <RankRow index={0} title="مشتریانی که تست گرفته‌اند" value={data.trial.trial_customers}
              max={Math.max(data.trial.trial_customers, 1)} color={C.sky}
              valueLabel={fmtNum(data.trial.trial_customers)} />
            <RankRow index={1} title="از میان آن‌ها، خرید کرده‌اند"
              value={data.trial.converted_customers}
              max={Math.max(data.trial.trial_customers, 1)} color={C.green}
              valueLabel={fmtNum(data.trial.converted_customers)} />
          </Stack>
          <Grid container spacing={1.2} sx={{ mt: 1.4 }}>
            <Grid item xs={6}>
              <Metric label="نرخ تبدیل" color={C.green}
                value={trialRate === null ? "—" : pct(trialRate)}
                hint="از کل تاریخچهٔ فروشگاه‌ها" />
            </Grid>
            <Grid item xs={6}>
              <Metric label="تست‌های این دوره" color={C.sky}
                value={fmtNum(data.services.trials_in_period)}
                hint={`${fmtNum(data.services.trials_active)} تست هنوز فعال`} />
            </Grid>
          </Grid>
        </SectionCard>
      </Grid>

      <Grid item xs={12}>
        <SectionCard title="نمای مشتریان">
          <Grid container spacing={1.5}>
            {[
              { label: "کل مشتریان", value: fmtNum(c.total), hint: `${fmtNum(c.new_in_period)} نفر در این دوره`, color: C.blue },
              { label: "فعال در ۷ روز", value: fmtNum(c.active_7d), hint: pct(share(c.active_7d, c.total)) + " از کل", color: C.green },
              { label: "فعال در ۳۰ روز", value: fmtNum(c.active_30d), hint: pct(share(c.active_30d, c.total)) + " از کل", color: C.teal },
              { label: "مشتری جدید امروز", value: fmtNum(c.new_today), color: C.sky },
              { label: "خریداران این دوره", value: fmtNum(c.buyers_in_period), hint: pct(share(c.buyers_in_period, c.total)) + " از کل مشتریان", color: C.violet },
              { label: "خرید تکراری", value: fmtNum(c.repeat_buyers_in_period), hint: "بیش از یک خرید در دوره", color: C.indigo },
              { label: "میانگین درآمد هر خریدار", value: fmtToman(c.arppu_toman), color: C.amber },
              { label: "مسدودشده", value: fmtNum(c.banned), hint: "توسط فروشنده", color: C.red },
            ].map((m) => (
              <Grid item xs={6} sm={4} lg={3} key={m.label}>
                <Metric {...m} />
              </Grid>
            ))}
          </Grid>
        </SectionCard>
      </Grid>

      <Grid item xs={12} lg={6}>
        <SectionCard title="کیف پول مشتریان (بدهی فروشندگان)">
          <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 1.5 }}>
            پولی که مشتریان شارژ کرده‌اند و هنوز خرج نکرده‌اند. این مبلغ تعهدِ فروشنده به مشتریانش
            است و ربطی به فاکتور شما ندارد.
          </Typography>
          <Grid container spacing={1.5}>
            <Grid item xs={6}>
              <Metric label="مجموع موجودی کیف پول‌ها" color={C.violet}
                value={fmtToman(c.wallet_liability_toman)} />
            </Grid>
            <Grid item xs={6}>
              <Metric label="شارژ تأییدشدهٔ این دوره" color={C.green}
                value={fmtToman(data.topups.confirmed_toman)}
                hint={`${fmtNum(data.topups.confirmed_count)} فقره`} />
            </Grid>
          </Grid>
        </SectionCard>
      </Grid>

      <Grid item xs={12} lg={6}>
        <SectionCard title="کدهای شارژ و هدیه">
          <Grid container spacing={1.5}>
            <Grid item xs={4}>
              <Metric label="کد فعال" value={fmtNum(data.credits.active_codes)} color={C.pink} />
            </Grid>
            <Grid item xs={4}>
              <Metric label="استفاده در دوره" value={fmtNum(data.credits.redemptions)} color={C.sky} />
            </Grid>
            <Grid item xs={4}>
              <Metric label="هدیهٔ پرداخت‌شده" value={fmtToman(data.credits.bonus_toman)} color={C.amber} />
            </Grid>
          </Grid>
        </SectionCard>
      </Grid>
    </Grid>
  );
}

function ServicesTab({ data }: { data: Analytics }) {
  const s = data.services;
  const slices = [
    { label: "فعال", value: s.provisioned, color: C.green },
    { label: "در حال تمدید", value: s.renewing, color: C.sky },
    { label: "غیرفعال‌شده", value: s.disabled, color: C.slate },
    { label: "ناتمام", value: s.pending, color: C.amber },
    { label: "ناموفق", value: s.failed, color: C.red },
    { label: "حذف‌شده", value: s.deleted, color: C.violet },
  ];
  const usedShare = share(s.used_gb, s.quota_gb);

  return (
    <Grid container spacing={2}>
      <Grid item xs={12} lg={5}>
        <SectionCard title="وضعیت سرویس‌های فروخته‌شده">
          <Donut slices={slices} total={s.total} centerLabel="سرویس"
            ariaLabel="توزیع وضعیت سرویس‌های فروشگاهی" />
        </SectionCard>
      </Grid>

      <Grid item xs={12} lg={7}>
        <SectionCard title="هشدارهای سرویس"
          action={<Chip size="small" label={`${fmtNum(s.active)} سرویس فعال`} sx={{ fontWeight: 700 }} />}>
          <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 1.5 }}>
            بر پایهٔ تاریخ و حجم واقعیِ کاربر روی پنل — همان محاسبه‌ای که یادآورهای ربات از آن
            استفاده می‌کنند.
          </Typography>
          <Grid container spacing={1.5}>
            <Grid item xs={6} sm={4}>
              <Metric label="تا ۳ روز دیگر منقضی" color={C.red} value={fmtNum(s.expiring_3d)}
                hint="فرصت تمدید" />
            </Grid>
            <Grid item xs={6} sm={4}>
              <Metric label="تا ۷ روز دیگر منقضی" color={C.amber} value={fmtNum(s.expiring_7d)} />
            </Grid>
            <Grid item xs={6} sm={4}>
              <Metric label="منقضی‌شده ولی فعال" color={C.slate} value={fmtNum(s.expired)}
                hint="هنوز روی پنل باقی است" />
            </Grid>
            <Grid item xs={6} sm={4}>
              <Metric label="بیش از ۸۰٪ حجم مصرف‌شده" color={C.violet} value={fmtNum(s.high_usage)} />
            </Grid>
            <Grid item xs={6} sm={4}>
              <Metric label="تمدید خودکار مسلح" color={C.green} value={fmtNum(s.autorenew_armed)}
                hint="مبلغ در کیف پول رزرو شده" />
            </Grid>
            <Grid item xs={6} sm={4}>
              <Metric label="تست رایگان فعال" color={C.sky} value={fmtNum(s.trials_active)} />
            </Grid>
          </Grid>
        </SectionCard>
      </Grid>

      <Grid item xs={12} lg={6}>
        <SectionCard title="حجم فروخته‌شده در برابر مصرف">
          <Stack spacing={2}>
            <RankRow index={0} title="حجم فروخته‌شدهٔ سرویس‌های فعال" value={s.quota_gb}
              max={Math.max(s.quota_gb, 1)} color={C.blue} valueLabel={fmtGb(s.quota_gb)} />
            <RankRow index={1} title="حجم مصرف‌شده" value={s.used_gb}
              max={Math.max(s.quota_gb, 1)} color={C.teal} valueLabel={fmtGb(s.used_gb)} />
          </Stack>
          <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 1.8 }}>
            {pct(usedShare)} از حجمی که مشتریان خریده‌اند مصرف شده است. سرویس‌های بدون نمونهٔ
            هم‌گام‌شده روی پنل در این محاسبه نیستند.
          </Typography>
        </SectionCard>
      </Grid>

      <Grid item xs={12} lg={6}>
        <SectionCard title="سلامت عملیات خرید و تمدید"
          action={data.operations.failed_24h > 0
            ? <Chip size="small" color="error" label={`${fmtNum(data.operations.failed_24h)} خطا در ۲۴ ساعت`} />
            : <Chip size="small" color="success" variant="outlined" label="بدون خطای تازه" />}>
          <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 1.5 }}>
            هر خرید یا تمدید یک «عملیات» ماندگار می‌سازد تا دوبار پول کم نشود. ماندن در حالت
            «در جریان» یعنی فرایندی نیمه‌کاره مانده است.
          </Typography>
          <Grid container spacing={1.5}>
            <Grid item xs={6} sm={4}>
              <Metric label="موفق" value={fmtNum(data.operations.done)} color={C.green} />
            </Grid>
            <Grid item xs={6} sm={4}>
              <Metric label="در جریان" value={fmtNum(data.operations.in_progress)} color={C.amber} />
            </Grid>
            <Grid item xs={6} sm={4}>
              <Metric label="آغازنشده" value={fmtNum(data.operations.pending)} color={C.slate} />
            </Grid>
            <Grid item xs={6} sm={4}>
              <Metric label="ناموفق" value={fmtNum(data.operations.failed)} color={C.red} />
            </Grid>
            <Grid item xs={6} sm={4}>
              <Metric label="برگشت‌خورده" value={fmtNum(data.operations.reversed)} color={C.violet}
                hint="پول به کیف پول برگشته" />
            </Grid>
            <Grid item xs={6} sm={4}>
              <Metric label="سرویس ناتمام" value={fmtNum(data.services.pending)} color={C.slate} />
            </Grid>
          </Grid>
        </SectionCard>
      </Grid>
    </Grid>
  );
}

function OverviewTab({ data }: { data: Analytics }) {
  const b = data.bots;
  const slices = [
    { label: "در حال فروش", value: b.selling, color: C.green },
    { label: "موقتاً بسته", value: b.closed, color: C.amber },
    { label: "خطای توکن", value: b.errored, color: C.red },
    { label: "خاموش", value: b.disabled, color: C.slate },
  ];
  const attention = data.shops
    .map((r) => {
      const idle = daysAgo(r.last_sale_at);
      const reasons: string[] = [];
      if (r.status === "errored") reasons.push("توکن ربات کار نمی‌کند");
      if (!r.enabled) reasons.push("ربات خاموش است");
      if (r.shop_closed) reasons.push("فروشگاه موقتاً بسته است");
      if (r.health_error_class === "unauthorized") reasons.push("دسترسی ربات رد شد");
      if (!r.plans) reasons.push("هیچ پلن فعالی ندارد");
      if (r.pending_topups_count) reasons.push(`${fmtNum(r.pending_topups_count)} شارژ در انتظار تأیید`);
      if (r.expiring_3d) reasons.push(`${fmtNum(r.expiring_3d)} سرویس تا ۳ روز دیگر منقضی می‌شود`);
      if (idle === null && r.customers) reasons.push("هنوز هیچ فروشی نداشته");
      else if (idle !== null && idle >= 14) reasons.push(`${fmtNum(idle)} روز است فروشی نداشته`);
      return { row: r, reasons };
    })
    .filter((x) => x.reasons.length)
    .slice(0, 12);

  const topShops = data.shops.filter((r) => r.net_sales_toman > 0).slice(0, 10);
  const maxShop = Math.max(...topShops.map((r) => r.net_sales_toman), 1);
  const adoption = share(b.total, b.eligible_resellers);

  return (
    <Grid container spacing={2}>
      <Grid item xs={12} lg={5}>
        <SectionCard title="وضعیت ربات‌ها"
          action={<Chip size="small" label={`${fmtNum(b.total)} ربات`} sx={{ fontWeight: 700 }} />}>
          <Donut slices={slices} total={b.total} centerLabel="ربات فروشگاهی"
            ariaLabel="توزیع وضعیت ربات‌های فروشگاهی" />
          <Grid container spacing={1.2} sx={{ mt: 1.4 }}>
            <Grid item xs={6}>
              <Metric label="پذیرش در میان نمایندگان" color={C.blue} value={pct(adoption)}
                hint={`${fmtNum(b.total)} از ${fmtNum(b.eligible_resellers)} نمایندهٔ مجاز`} />
            </Grid>
            <Grid item xs={6}>
              <Metric label="راه‌اندازی در این دوره" color={C.green} value={fmtNum(b.new_in_period)} />
            </Grid>
          </Grid>
        </SectionCard>
      </Grid>

      <Grid item xs={12} lg={7}>
        <SectionCard title="۱۰ فروشگاه برتر دوره"
          action={<Typography variant="caption" color="text.secondary">بر اساس فروش خالص</Typography>}>
          {topShops.length ? (
            <Stack spacing={2}>
              {topShops.map((r, i) => (
                <RankRow key={r.shop_id} index={i} title={r.reseller_name}
                  caption={`${r.bot_username ? `@${r.bot_username}` : r.panel_key} · ${fmtNum(r.orders)} سفارش · ${fmtNum(r.customers)} مشتری`}
                  value={r.net_sales_toman} max={maxShop}
                  valueLabel={fmtToman(r.net_sales_toman)} />
              ))}
            </Stack>
          ) : <EmptyState>در این دوره هیچ فروشگاهی فروش نداشته است.</EmptyState>}
        </SectionCard>
      </Grid>

      <Grid item xs={12} lg={6}>
        <SectionCard title="پیکربندی فروشگاه‌ها">
          <Grid container spacing={1.5}>
            <Grid item xs={6} sm={4}>
              <Metric label="بدون پلن فعال" value={fmtNum(b.without_plans)} color={C.amber}
                hint="نمی‌تواند بفروشد" />
            </Grid>
            <Grid item xs={6} sm={4}>
              <Metric label="تست رایگان روشن" value={fmtNum(b.trial_enabled)} color={C.sky} />
            </Grid>
            <Grid item xs={6} sm={4}>
              <Metric label="عضویت اجباری کانال" value={fmtNum(b.channel_locked)} color={C.violet} />
            </Grid>
            <Grid item xs={6} sm={4}>
              <Metric label="پنل ناسالم" value={fmtNum(b.panel_unhealthy)} color={C.red}
                hint="ساخت سرویس مختل می‌شود" />
            </Grid>
            <Grid item xs={6} sm={4}>
              <Metric label="خطای توکن" value={fmtNum(b.errored)} color={C.red} />
            </Grid>
            <Grid item xs={6} sm={4}>
              <Metric label="موقتاً بسته" value={fmtNum(b.closed)} color={C.amber} />
            </Grid>
          </Grid>
        </SectionCard>
      </Grid>

      <Grid item xs={12} lg={6}>
        <SectionCard title="نیازمند رسیدگی"
          action={<Chip size="small" color={attention.length ? "warning" : "success"}
            variant="outlined" label={attention.length ? `${fmtNum(attention.length)} فروشگاه` : "همه‌چیز مرتب"} />}>
          {attention.length ? (
            <Stack spacing={1.2}>
              {attention.map(({ row, reasons }) => (
                <Box key={row.shop_id} sx={{
                  p: 1.4, borderRadius: 2.5, border: 1, borderColor: "divider",
                }}>
                  <Stack direction="row" justifyContent="space-between" alignItems="center" spacing={1}>
                    <Typography variant="body2" noWrap sx={{ fontWeight: 750 }}>
                      {row.reseller_name}
                    </Typography>
                    <ShopStatus row={row} />
                  </Stack>
                  <Typography variant="caption" color="text.secondary">
                    {reasons.join(" · ")}
                  </Typography>
                </Box>
              ))}
            </Stack>
          ) : <EmptyState>هیچ فروشگاهی هشدار باز ندارد.</EmptyState>}
        </SectionCard>
      </Grid>
    </Grid>
  );
}

// ── page ────────────────────────────────────────────────────────────────────────────────
const TABS = [
  { label: "نمای کلی" },
  { label: "فروش" },
  { label: "مشتریان" },
  { label: "سرویس‌ها" },
  { label: "فروشگاه‌ها" },
];

export default function StorefrontAnalytics() {
  const [period, setPeriod] = useState(currentPeriod());
  const [tab, setTab] = useState(0);
  // The report is cached server-side for a minute (it fans out into ~15 aggregate queries). A
  // background refetch may serve that cache, but pressing «به‌روزرسانی» must not — the flag rides
  // on a ref so forcing a recompute doesn't change the query key and blow away the cached view.
  const forceRefresh = useRef(false);
  const { data, isLoading, isError, refetch, isFetching } = useQuery({
    queryKey: ["storefront-analytics", period],
    queryFn: () => {
      const refresh = forceRefresh.current;
      forceRefresh.current = false;
      return getStorefrontAnalytics(period, refresh);
    },
  });

  if (isError) {
    return (
      <Alert severity="error"
        action={<Button color="inherit" size="small" onClick={() => refetch()}>تلاش دوباره</Button>}>
        خطا در بارگذاری آمار ربات‌های فروشگاهی. اتصال را بررسی کنید.
      </Alert>
    );
  }

  const trialRate = data?.trial.rate == null ? null : data.trial.rate * 100;

  return (
    <Box>
      <Stack direction={{ xs: "column", sm: "row" }} justifyContent="space-between"
        alignItems={{ xs: "stretch", sm: "center" }} spacing={1.5} sx={{ mb: 2.5 }}>
        <Box>
          <Typography variant="h5">آنالیز ربات‌های فروشگاهی</Typography>
          <Typography variant="body2" color="text.secondary" sx={{ mt: 0.4 }}>
            وضعیت لحظه‌ای و کارنامهٔ ماهانهٔ فروشگاه‌های نمایندگان — فروش، مشتری، سرویس و سلامت ربات‌ها
          </Typography>
        </Box>
        <Stack direction="row" spacing={1} alignItems="center">
          <PeriodPicker value={period} onChange={setPeriod} />
          <Tooltip title="به‌روزرسانی">
            <span>
              <IconButton
                onClick={() => { forceRefresh.current = true; refetch(); }}
                disabled={isFetching} size="small"
              >
                <RefreshIcon />
              </IconButton>
            </span>
          </Tooltip>
        </Stack>
      </Stack>

      {isLoading || !data ? (
        <>
          <Grid container spacing={2}>
            {[0, 1, 2, 3, 4, 5, 6, 7].map((i) => (
              <Grid item xs={12} sm={6} lg={3} key={i}>
                <Skeleton variant="rounded" height={154} animation="wave" sx={{ borderRadius: "18px" }} />
              </Grid>
            ))}
          </Grid>
          <Skeleton variant="rounded" height={420} animation="wave"
            sx={{ borderRadius: "18px", mt: 2 }} />
        </>
      ) : (
        <>
          <Grid container spacing={2}>
            {[
              {
                label: "ربات‌های در حال فروش",
                value: <CountUp to={data.bots.selling} format={fmtNum} />,
                sub: <Hint>{fmtNum(data.bots.total)} ربات ثبت‌شده · {fmtNum(data.bots.errored)} خطادار</Hint>,
                color: C.blue, icon: <StorefrontIcon />,
              },
              {
                label: "فروش امروز",
                value: <CountUp to={data.sales_today.net_toman} format={fmtToman} />,
                sub: <Delta current={data.sales_today.net_toman}
                  previous={data.sales_yesterday.net_toman} label="دیروز" />,
                color: C.green, icon: <TrendingUpIcon />,
              },
              {
                label: "فروش این دوره",
                value: <CountUp to={data.sales_period.net_toman} format={fmtToman} />,
                sub: <Delta current={data.sales_period.net_toman}
                  previous={data.sales_previous_period.net_toman} label="دورهٔ قبل" />,
                color: C.teal, icon: <TrendingUpIcon />,
              },
              {
                label: "سفارش‌های این دوره",
                value: <CountUp to={data.sales_period.orders} format={fmtNum} />,
                sub: <Hint>میانگین هر سفارش {fmtToman(data.customers.avg_order_toman)}</Hint>,
                color: C.amber, icon: <VpnKeyIcon />,
              },
              {
                label: "مشتریان فعال (۳۰ روز)",
                value: <CountUp to={data.customers.active_30d} format={fmtNum} />,
                sub: <Hint>{fmtNum(data.customers.total)} مشتری کل · {fmtNum(data.customers.new_today)} جدید امروز</Hint>,
                color: C.sky, icon: <GroupIcon />,
              },
              {
                label: "سرویس‌های فعال",
                value: <CountUp to={data.services.active} format={fmtNum} />,
                sub: <Hint color={data.services.expiring_3d ? undefined : undefined}>
                  {fmtNum(data.services.expiring_3d)} سرویس تا ۳ روز دیگر منقضی می‌شود
                </Hint>,
                color: C.violet, icon: <AutorenewIcon />,
              },
              {
                label: "کیف پول مشتریان",
                value: <CountUp to={data.customers.wallet_liability_toman} format={fmtToman} />,
                sub: <Hint>{fmtNum(data.topups.pending_count)} شارژ در انتظار تأیید</Hint>,
                color: C.pink, icon: <WalletIcon />,
              },
              {
                label: "نرخ تبدیل تست به خرید",
                value: trialRate === null ? "—" : pct(trialRate),
                sub: <Hint>{fmtNum(data.trial.converted_customers)} از {fmtNum(data.trial.trial_customers)} مشتریِ تست</Hint>,
                color: C.indigo, icon: <HourglassEmptyIcon />,
              },
            ].map((card) => (
              <Grid item xs={12} sm={6} lg={3} key={card.label}>
                <StatCard {...card} />
              </Grid>
            ))}
          </Grid>

          {(data.bots.errored > 0 || data.operations.failed_24h > 0 || data.topups.pending_count > 0) && (
            <Alert severity={data.bots.errored ? "warning" : "info"} icon={<WarningAmberIcon />}
              sx={{ mt: 2 }}>
              {[
                data.bots.errored ? `${fmtNum(data.bots.errored)} ربات با توکن خراب` : null,
                data.operations.failed_24h ? `${fmtNum(data.operations.failed_24h)} عملیات ناموفق در ۲۴ ساعت گذشته` : null,
                data.topups.pending_count ? `${fmtNum(data.topups.pending_count)} شارژ در انتظار تأیید فروشنده` : null,
              ].filter(Boolean).join(" — ")}
            </Alert>
          )}

          <Box sx={{ mt: 2.5, mb: 2 }}>
            <SegmentedTabs value={tab} onChange={setTab} tabs={TABS} />
          </Box>

          <Reveal key={`${tab}-${period}`}>
            {tab === 0 && <OverviewTab data={data} />}
            {tab === 1 && <SalesTab data={data} />}
            {tab === 2 && <CustomersTab data={data} />}
            {tab === 3 && <ServicesTab data={data} />}
            {tab === 4 && <ShopsTable rows={data.shops} />}
          </Reveal>

          <Typography variant="caption" color="text.secondary"
            sx={{ display: "block", mt: 2.5, textAlign: "center" }}>
            آخرین محاسبه: {fmtDateTime(data.generated_at)} — ارقام «امروز» و «۷/۳۰ روز اخیر» همیشه
            نسبت به همین لحظه‌اند، بقیهٔ ارقام مربوط به دورهٔ {data.period} است.
          </Typography>
        </>
      )}
    </Box>
  );
}
