import { useState } from "react";
import {
  Card, Grid, Stack, Table, TableBody, TableCell, TableHead, TableRow, Typography,
} from "@mui/material";
import DataUsageIcon from "@mui/icons-material/esm/DataUsage";
import ReceiptLongIcon from "@mui/icons-material/esm/ReceiptLong";
import ShoppingCartIcon from "@mui/icons-material/esm/ShoppingCart";
import TrendingUpIcon from "@mui/icons-material/esm/TrendingUp";
import { useQuery } from "@tanstack/react-query";
import { useTheme } from "@mui/material/styles";
import { useOutletContext } from "react-router-dom";
import { DataState } from "../../components/DataState";
import PeriodPicker from "../../components/PeriodPicker";
import StatCard from "../../components/StatCard";
import EChart from "../../components/EChart";
import { financeTrendOption } from "../financeTrend";
import { fmtGb, fmtNum, fmtToman } from "../../format";
import { EmptyState, SectionCard } from "../ui";
import { getStorefrontFinance, storefrontQueryKeys } from "./api";
import type { StorefrontFinancePeriod } from "./types";
import type { StorefrontOutletContext } from "./StorefrontShell";

const EMPTY: StorefrontFinancePeriod = {
  label: "", purchases: 0, renewals: 0, gb_sold: 0, gb_free: 0, gb_billable: 0, cost_toman: 0,
  gross_sales_toman: 0, reversals_toman: 0, net_sales_toman: 0, profit_toman: 0, unresolved_ops: 0,
};

const CHART_MONTHS = 12;
const faDigits = (s: string) => s.replace(/[0-9]/g, (d) => "۰۱۲۳۴۵۶۷۸۹"[+d]);

// The reseller buys quota from us at a fixed price per GB and resells it through their bot. This
// page is the only place that puts both halves side by side: what the bot collected, and what the
// quota behind it costs on their own invoice. Everything is derived from ONE payload holding every
// month, so changing the period is instant and never refetches.
export default function StorefrontFinancePage() {
  const { shop } = useOutletContext<StorefrontOutletContext>();
  const theme = useTheme();
  const query = useQuery({
    queryKey: storefrontQueryKeys.finance(shop.id),
    queryFn: () => getStorefrontFinance(shop.id),
  });
  const d = query.data;

  // Null until the user picks: default to the newest month that HAS activity, so a shop whose
  // current month is still empty doesn't open on a page of zeros. "" is the explicit «همه».
  const [picked, setPicked] = useState<string | null>(null);
  const period = picked ?? d?.months[0]?.label ?? "";
  const isAll = period === "";
  const months = d?.months ?? [];
  const current = isAll
    ? (d?.totals ?? EMPTY)
    : months.find((m) => m.label === period) ?? { ...EMPTY, label: period };
  const hasRows = isAll ? months.length > 0 : current.purchases + current.renewals > 0;

  const rate = d?.cost_per_gb_toman ?? 0;
  const transactions = current.purchases + current.renewals;
  const margin = current.net_sales_toman > 0
    ? Math.round((current.profit_toman / current.net_sales_toman) * 1000) / 10
    : null;
  const profitable = current.profit_toman >= 0;
  // Oldest month on the left, so the bars read as a timeline.
  const chartRows = months.slice(0, CHART_MONTHS).slice().reverse();

  return (
    <DataState isLoading={query.isLoading} isError={query.isError} rows={6} onRetry={() => query.refetch()}>
      {d && (
        <Stack spacing={2}>
          <Stack
            direction={{ xs: "column", sm: "row" }}
            justifyContent="space-between"
            alignItems={{ xs: "stretch", sm: "center" }}
            spacing={1.5}
          >
            <Stack>
              <Typography variant="h5">گزارش مالی</Typography>
              <Typography variant="body2" color="text.secondary" sx={{ mt: 0.4 }}>
                {isAll
                  ? "مجموع همهٔ ماه‌ها"
                  : `ماه ${faDigits(period)}`}
                {rate > 0 && ` · نرخ خرید شما: ${fmtToman(rate)} به ازای هر گیگابایت`}
              </Typography>
            </Stack>
            <PeriodPicker value={period} onChange={setPicked} allowEmpty />
          </Stack>

          <Grid container spacing={2}>
            <Grid item xs={12} sm={6} md={3}>
              <StatCard
                label="گیگ محاسبه‌شده"
                value={fmtGb(current.gb_billable)}
                sub={current.gb_free > 0
                  ? `از ${fmtGb(current.gb_sold)} فروش، ${fmtGb(current.gb_free)} تست رایگان محاسبه نشد`
                  : `${fmtNum(transactions)} خرید و تمدید`}
                icon={<DataUsageIcon />}
                color="#0ea5e9"
              />
            </Grid>
            <Grid item xs={12} sm={6} md={3}>
              <StatCard
                label="هزینه در فاکتور شما"
                value={fmtToman(current.cost_toman)}
                sub={rate > 0 ? `${fmtGb(current.gb_billable)} × ${fmtToman(rate)}` : "بدون هزینه"}
                icon={<ReceiptLongIcon />}
                color="#f43f5e"
              />
            </Grid>
            <Grid item xs={12} sm={6} md={3}>
              <StatCard
                label="دریافتی از ربات"
                value={fmtToman(current.net_sales_toman)}
                sub={current.reversals_toman > 0
                  ? `${fmtToman(current.gross_sales_toman)} منهای ${fmtToman(current.reversals_toman)} برگشتی`
                  : `${fmtNum(transactions)} تراکنش`}
                icon={<ShoppingCartIcon />}
                color="#0071e3"
              />
            </Grid>
            <Grid item xs={12} sm={6} md={3}>
              <StatCard
                label={profitable ? "سود" : "زیان"}
                value={fmtToman(Math.abs(current.profit_toman))}
                sub={margin === null ? undefined : `حاشیهٔ سود ${fmtNum(margin)}٪`}
                icon={<TrendingUpIcon />}
                color={profitable ? "#10b981" : "#ff3b30"}
              />
            </Grid>
          </Grid>

          {months.length === 0 ? (
            <Card><EmptyState>هنوز فروشی در این فروشگاه ثبت نشده است.</EmptyState></Card>
          ) : (
            <>
              <SectionCard title="روند ماهانه">
                <EChart
                  option={financeTrendOption(theme, chartRows)}
                  height={280}
                  ariaLabel="نمودار دریافتی و هزینه به تفکیک ماه"
                />
              </SectionCard>

              <Card sx={{ overflowX: "auto" }}>
                <Table size="small" className="resp-table">
                  <TableHead>
                    <TableRow>
                      <TableCell>ماه</TableCell>
                      <TableCell align="left">خرید</TableCell>
                      <TableCell align="left">تمدید</TableCell>
                      <TableCell align="left">گیگ</TableCell>
                      <TableCell align="left">هزینه</TableCell>
                      <TableCell align="left">دریافتی</TableCell>
                      <TableCell align="left">سود / زیان</TableCell>
                    </TableRow>
                  </TableHead>
                  <TableBody>
                    {months.map((m) => (
                      <TableRow
                        key={m.label}
                        hover
                        selected={!isAll && m.label === period}
                        onClick={() => setPicked(m.label)}
                        sx={{ cursor: "pointer" }}
                      >
                        <TableCell dir="ltr" sx={{ fontWeight: 700, fontFamily: "monospace" }}>
                          {m.label}
                        </TableCell>
                        <TableCell align="left">{fmtNum(m.purchases)}</TableCell>
                        <TableCell align="left">{fmtNum(m.renewals)}</TableCell>
                        <TableCell align="left">{fmtGb(m.gb_billable)}</TableCell>
                        <TableCell align="left" sx={{ whiteSpace: "nowrap" }}>
                          {fmtToman(m.cost_toman)}
                        </TableCell>
                        <TableCell align="left" sx={{ whiteSpace: "nowrap" }}>
                          {fmtToman(m.net_sales_toman)}
                        </TableCell>
                        <TableCell
                          align="left"
                          sx={{
                            fontWeight: 750,
                            whiteSpace: "nowrap",
                            color: m.profit_toman >= 0 ? "success.main" : "error.main",
                          }}
                        >
                          {fmtToman(m.profit_toman)}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </Card>
            </>
          )}

          {!hasRows && months.length > 0 && (
            <Card><EmptyState>در این ماه فعالیتی ثبت نشده است.</EmptyState></Card>
          )}

          <SectionCard title="این عدد چطور حساب می‌شود؟">
            <Stack component="ul" spacing={1} sx={{ m: 0, pr: 2.5, color: "text.secondary" }}>
              <Typography component="li" variant="body2">
                هزینه = گیگ فروخته‌شده (خرید + تمدید) ضربدر نرخ خرید شما. سرویس‌های تستِ رایگان و
                پلن‌های {fmtGb(d.excluded_below_gb)} و کمتر — که در فاکتور شما هم رایگان‌اند — حساب
                نمی‌شوند.
              </Typography>
              <Typography component="li" variant="body2">
                تمدیدهایی که خودِ مدیر فروشگاه برای مشتری انجام می‌دهد رایگان‌اند؛ گیگ و هزینه دارند
                ولی دریافتی ندارند.
              </Typography>
              <Typography component="li" variant="body2">
                ماه ثبت ممکن است یک ماه جابه‌جا شود: فاکتور، سرویس را در ماهی حساب می‌کند که مشتری
                بعد از تمدید دوباره وصل شود. اگر یک سرویس در یک ماه دو بار تمدید شود، اینجا دو فروش
                است ولی فاکتور آن را یک بار حساب می‌کند.
              </Typography>
              <Typography component="li" variant="body2">
                مصرف اضافه و ارتقای پلن، خطوط جداگانه‌ای در فاکتورند که در این گزارش نیامده‌اند؛ پس
                این عدد یک کف است، نه رقم نهایی فاکتور.
              </Typography>
              {d.totals.unresolved_ops > 0 && (
                <Typography component="li" variant="body2">
                  {fmtNum(d.totals.unresolved_ops)} تراکنش قدیمی که پلن و سرویسش هر دو حذف شده‌اند،
                  حجمشان قابل بازیابی نبود و در گیگ محاسبه نشده‌اند.
                </Typography>
              )}
            </Stack>
          </SectionCard>
        </Stack>
      )}
    </DataState>
  );
}
