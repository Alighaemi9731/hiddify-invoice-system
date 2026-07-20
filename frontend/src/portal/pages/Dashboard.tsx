import { useState } from "react";
import { Alert, Box, Button, Chip, Grid, Skeleton, Stack, Typography } from "@mui/material";
import { alpha, useTheme } from "@mui/material/styles";
import TrendingUpIcon from "@mui/icons-material/esm/TrendingUp";
import DataUsageIcon from "@mui/icons-material/esm/DataUsage";
import WarningAmberIcon from "@mui/icons-material/esm/WarningAmber";
import GroupIcon from "@mui/icons-material/esm/Group";
import PaymentIcon from "@mui/icons-material/esm/Payment";
import { useQuery } from "@tanstack/react-query";
import { portalSummary, portalPayOptionsAll, portalSalesByMonth } from "../portalClient";
import { usePortalAuth } from "../PortalAuthContext";
import PayDialog from "../PayDialog";
import StatCard, { currentPeriod } from "../../components/StatCard";
import PeriodPicker from "../../components/PeriodPicker";
import EChart from "../../components/EChart";
import { CountUp, Reveal } from "../../components/motion";
import { fmtGb, fmtNum, fmtToman } from "../../format";
import { SectionCard, EmptyState } from "../ui";
import { dailyTrendOption } from "../dailyTrend";
import { monthlyTrendOption } from "../monthlyTrend";

const METHOD_LABELS: [keyof PayMethods, string][] = [
  ["card", "کارت به کارت"],
  ["usdt", "USDT (BEP-20)"],
  ["ton", "TON"],
  ["avax", "AVAX"],
  ["screenshot", "رسیدِ تصویری"],
];
type PayMethods = { usdt: boolean; card: boolean; ton: boolean; avax: boolean; screenshot: boolean };

export default function PortalDashboard() {
  const [period, setPeriod] = useState(currentPeriod());
  const [payAll, setPayAll] = useState(false);
  const theme = useTheme();
  const isDark = theme.palette.mode === "dark";
  const { resellers } = usePortalAuth();
  const enforcedNames = resellers.filter((r) => r.enforcement_state === "enforced").map((r) => r.name);
  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ["portal-summary", period],
    queryFn: () => portalSummary(period),
  });
  // The enabled payment methods (returned even with zero payable invoices), for the info chips.
  const { data: payOpts } = useQuery({
    queryKey: ["portal-pay-options-all"],
    queryFn: () => portalPayOptionsAll(),
  });
  const methods = payOpts?.methods;
  const hasOutstanding = !!data && data.outstanding.count > 0;

  const trend = data?.trend || [];
  const perReseller = data?.per_reseller || [];
  const trendEmpty = !trend.length || trend.every((d) => !d.amount_toman);
  const trendOption = dailyTrendOption(theme, trend);

  const { data: monthly } = useQuery({
    queryKey: ["portal-sales-by-month"],
    queryFn: () => portalSalesByMonth(6),
  });
  const monthlyRows = monthly?.months || [];
  const monthlyEmpty = !monthlyRows.length || monthlyRows.every((m) => !m.amount_toman);
  const monthlyOption = monthlyTrendOption(theme, monthlyRows);
  const deltaPct = monthly?.summary.delta_pct ?? null;

  const [welcome, setWelcome] = useState(() => !localStorage.getItem("portal_welcome_dismissed"));
  const dismissWelcome = () => { localStorage.setItem("portal_welcome_dismissed", "1"); setWelcome(false); };

  return (
    <Box>
      {enforcedNames.length > 0 && (
        <Alert
          severity="error"
          sx={{ mb: 2 }}
          action={
            hasOutstanding ? (
              <Button color="inherit" size="small" onClick={() => setPayAll(true)}>پرداخت بدهی</Button>
            ) : undefined
          }
        >
          نمایندگیِ «{enforcedNames.join("، ")}» مسدود است. برای رفعِ مسدودی، بدهیِ معوق را پرداخت کنید.
        </Alert>
      )}
      {welcome && (
        <Alert severity="info" onClose={dismissWelcome} sx={{ mb: 2.5 }}>
          به پنلِ نماینده خوش آمدید. در این پنل می‌توانید فاکتورها را مشاهده و پرداخت کنید، زیرمجموعه‌ها و
          ظرفیتِ خود را مدیریت کنید و با پشتیبانی در ارتباط باشید. برای آشناییِ بیشتر، بخشِ «راهنما» را ببینید.
        </Alert>
      )}
      <Stack
        direction={{ xs: "column", sm: "row" }}
        justifyContent="space-between"
        alignItems={{ xs: "stretch", sm: "center" }}
        spacing={1.5}
        sx={{ mb: 2.5 }}
      >
        <Box>
          <Typography variant="h5">نمای کلی شما</Typography>
          <Typography variant="body2" color="text.secondary" sx={{ mt: 0.4 }}>
            برآوردِ فروشِ ماهِ جاری (شما + زیرمجموعه‌ها) و بدهیِ معوق
          </Typography>
        </Box>
        <Stack direction="row" spacing={1} alignItems="center">
          {hasOutstanding && (
            <Button
              variant="contained"
              color="success"
              startIcon={<PaymentIcon />}
              onClick={() => setPayAll(true)}
            >
              پرداختِ بدهی ({fmtNum(data!.outstanding.count)} فاکتور)
            </Button>
          )}
          <PeriodPicker value={period} onChange={setPeriod} />
        </Stack>
      </Stack>

      {isError ? (
        <Alert
          severity="error"
          action={<Button color="inherit" size="small" onClick={() => refetch()}>تلاش دوباره</Button>}
        >
          خطا در بارگذاری اطلاعات. اتصال را بررسی کنید.
        </Alert>
      ) : isLoading || !data ? (
        <Grid container spacing={2}>
          {[0, 1, 2, 3].map((i) => (
            <Grid item xs={12} sm={6} lg={3} key={i}>
              <Skeleton variant="rounded" height={154} animation="wave" sx={{ borderRadius: "18px" }} />
            </Grid>
          ))}
        </Grid>
      ) : (
        <>
          <Grid container spacing={2}>
            {[
              {
                label: "برآورد فروش ماه",
                value: <CountUp to={data.estimate.amount_toman} format={fmtToman} />,
                sub: <Typography variant="caption" color="text.secondary">دورهٔ {data.period}</Typography>,
                color: "#10b981",
                icon: <TrendingUpIcon />,
              },
              {
                label: "حجم فروخته‌شده",
                value: <CountUp to={data.estimate.gb} format={fmtGb} />,
                sub: <Typography variant="caption" color="text.secondary">{fmtNum(data.estimate.users)} سرویس</Typography>,
                color: "#0071e3",
                icon: <DataUsageIcon />,
              },
              {
                label: "بدهی معوق",
                value: <CountUp to={data.outstanding.amount_toman} format={fmtToman} />,
                sub: <Typography variant="caption" color="text.secondary">{fmtNum(data.outstanding.count)} فاکتور پرداخت‌نشده</Typography>,
                color: "#f43f5e",
                icon: <WarningAmberIcon />,
              },
              {
                label: "نمایندگی‌های شما",
                value: <CountUp to={data.reseller_count} format={fmtNum} />,
                sub: <Typography variant="caption" color="text.secondary">روی همهٔ پنل‌ها</Typography>,
                color: "#0ea5e9",
                icon: <GroupIcon />,
              },
            ].map((card) => (
              <Grid item xs={12} sm={6} lg={3} key={card.label}>
                <StatCard {...card} />
              </Grid>
            ))}
          </Grid>

          {methods && (
            <Stack direction="row" spacing={1} sx={{ mt: 2, flexWrap: "wrap", rowGap: 1, alignItems: "center" }}>
              <Typography variant="caption" color="text.secondary">روش‌های پرداخت:</Typography>
              {METHOD_LABELS.filter(([k]) => methods[k]).map(([k, label]) => (
                <Chip key={k} size="small" variant="outlined" label={label} />
              ))}
            </Stack>
          )}

          <Reveal delay={0.2}>
            <Grid container spacing={2} sx={{ mt: 0.5 }}>
              <Grid item xs={12} lg={7}>
                <SectionCard title="روند فروش روزانه" action={<Chip size="small" label={data.period} sx={{ fontWeight: 700 }} />}>
                  <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 1.5 }}>
                    سهمِ هر روز از فروشِ این ماه (بر اساس تاریخِ ساختِ سرویس‌ها)
                  </Typography>
                  {trendEmpty ? (
                    <EmptyState>فروشی در این دوره ثبت نشده است.</EmptyState>
                  ) : (
                    <EChart option={trendOption} height={290} ariaLabel="روند فروش روزانه" />
                  )}
                </SectionCard>
              </Grid>

              <Grid item xs={12} lg={5}>
                <SectionCard title="تفکیک بر اساس نمایندگی">
                  {perReseller.length ? (
                    <Stack spacing={1.4}>
                      {perReseller.map((r) => (
                        <Stack
                          key={r.id}
                          direction="row"
                          alignItems="center"
                          justifyContent="space-between"
                          spacing={1.5}
                          sx={{ p: 1.3, borderRadius: 2, bgcolor: alpha(theme.palette.text.secondary, isDark ? 0.06 : 0.05) }}
                        >
                          <Box sx={{ minWidth: 0 }}>
                            <Typography variant="body2" noWrap sx={{ fontWeight: 750 }}>{r.name}</Typography>
                            <Typography variant="caption" color="text.secondary">
                              {r.panel_key} · {fmtGb(r.gb)} · {fmtNum(r.users)} سرویس
                            </Typography>
                          </Box>
                          <Typography variant="body2" sx={{ fontWeight: 800, whiteSpace: "nowrap" }}>
                            {fmtToman(r.amount_toman)}
                          </Typography>
                        </Stack>
                      ))}
                    </Stack>
                  ) : (
                    <EmptyState>نمایندگی‌ای یافت نشد.</EmptyState>
                  )}
                </SectionCard>
              </Grid>
            </Grid>
          </Reveal>

          <Reveal delay={0.28}>
            <Box sx={{ mt: 2 }}>
              <SectionCard
                title="فروش ماهانه (شش ماه اخیر)"
                action={deltaPct != null ? (
                  <Chip
                    size="small"
                    color={deltaPct >= 0 ? "success" : "error"}
                    label={`${deltaPct >= 0 ? "▲" : "▼"} ${fmtNum(Math.abs(deltaPct))}٪ نسبت به ماه قبل`}
                    sx={{ fontWeight: 700 }}
                  />
                ) : undefined}
              >
                <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 1.5 }}>
                  فروشِ شما و زیرمجموعه‌ها در شش ماه اخیر
                </Typography>
                {monthlyEmpty ? (
                  <EmptyState>فروشی در ماه‌های اخیر ثبت نشده است.</EmptyState>
                ) : (
                  <EChart option={monthlyOption} height={300} ariaLabel="فروش ماهانه" />
                )}
              </SectionCard>
            </Box>
          </Reveal>
        </>
      )}

      <PayDialog invoice={null} payAll={payAll} onClose={() => setPayAll(false)} />
    </Box>
  );
}
