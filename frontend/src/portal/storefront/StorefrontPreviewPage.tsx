import { Alert, Box, Card, CardContent, Chip, Divider, Grid, Stack, Typography } from "@mui/material";
import PaymentsIcon from "@mui/icons-material/esm/Payments";
import RedeemIcon from "@mui/icons-material/esm/Redeem";
import SupportAgentIcon from "@mui/icons-material/esm/SupportAgent";
import { useQuery } from "@tanstack/react-query";
import { useOutletContext } from "react-router-dom";
import { DataState } from "../../components/DataState";
import { planLabel } from "./planLabel";
import { fmtNum, fmtToman } from "../../format";
import { getStorefrontPreview, storefrontQueryKeys } from "./api";
import type { StorefrontOutletContext } from "./StorefrontShell";

const PAYMENT_LABELS: Record<string, string> = { card: "کارت", usdt: "USDT", ton: "TON" };

export default function StorefrontPreviewPage() {
  const { shop } = useOutletContext<StorefrontOutletContext>();
  const query = useQuery({ queryKey: storefrontQueryKeys.preview(shop.id), queryFn: () => getStorefrontPreview(shop.id) });

  return (
    <Box>
      <Stack spacing={0.5} sx={{ mb: 2 }}>
        <Typography variant="h6">پیش‌نمایش مشتری</Typography>
        <Typography variant="body2" color="text.secondary">
          نمایش فقط‌خواندنی بر اساس تنظیمات فعلی؛ باز کردن این صفحه هیچ مشتری یا داده‌ای ایجاد نمی‌کند.
        </Typography>
      </Stack>
      <DataState isLoading={query.isLoading} isError={query.isError} rows={7} onRetry={() => query.refetch()}>
        {query.data && (
          <Box sx={{ maxWidth: 720, mx: "auto" }}>
            <Card sx={{ borderRadius: { xs: 3, sm: 5 }, overflow: "hidden" }}>
              <Box sx={{ bgcolor: "primary.main", color: "primary.contrastText", p: { xs: 2, sm: 3 } }}>
                <Typography variant="h6" sx={{ fontWeight: 850 }}>{shop.reseller.name}</Typography>
                <Typography variant="body2" sx={{ opacity: 0.88 }}>{query.data.welcome_text}</Typography>
              </Box>
              <CardContent sx={{ p: { xs: 2, sm: 3 } }}>
                <Stack spacing={2.5}>
                  {query.data.shop_closed && <Alert severity="warning">{query.data.closed_text || "فروشگاه موقتاً بسته است."}</Alert>}
                  {query.data.channel_required && <Alert severity="info">برای استفاده از فروشگاه، عضویت در کانال الزامی است.</Alert>}

                  {query.data.free_trial.enabled && (
                    <Stack direction="row" alignItems="center" spacing={1.25}>
                      <RedeemIcon color="secondary" />
                      <Box>
                        <Typography sx={{ fontWeight: 800 }}>تست رایگان</Typography>
                        <Typography variant="body2" color="text.secondary">{fmtNum(query.data.free_trial.gb)} گیگابایت · {fmtNum(query.data.free_trial.days)} روز</Typography>
                      </Box>
                    </Stack>
                  )}

                  <Divider />
                  <Typography sx={{ fontWeight: 800 }}>پلن‌های قابل خرید</Typography>
                  {!query.data.enabled_plans.length ? (
                    <Alert severity="info">فعلاً پلن فعالی وجود ندارد.</Alert>
                  ) : (
                    <Grid container spacing={1.5}>
                      {query.data.enabled_plans.map((plan) => (
                        <Grid item xs={12} sm={6} key={plan.id}>
                          <Card variant="outlined" sx={{ height: "100%" }}>
                            <CardContent>
                              <Typography sx={{ fontWeight: 800 }}>{planLabel(plan)}</Typography>
                              <Typography color="primary.main" sx={{ fontWeight: 850, mt: 0.75 }}>{fmtToman(plan.price_toman)}</Typography>
                            </CardContent>
                          </Card>
                        </Grid>
                      ))}
                    </Grid>
                  )}

                  <Divider />
                  <Stack direction={{ xs: "column", sm: "row" }} justifyContent="space-between" spacing={1.5}>
                    <Stack direction="row" alignItems="center" spacing={1}>
                      <PaymentsIcon color="action" />
                      <Box>
                        <Typography variant="body2" sx={{ fontWeight: 700 }}>روش‌های پرداخت</Typography>
                        <Stack direction="row" gap={0.75} flexWrap="wrap" sx={{ mt: 0.5 }}>
                          {query.data.payment_methods.map((method) => <Chip key={method} size="small" label={PAYMENT_LABELS[method] || method} />)}
                        </Stack>
                      </Box>
                    </Stack>
                    {query.data.support_contact && (
                      <Stack direction="row" alignItems="center" spacing={1}>
                        <SupportAgentIcon color="action" />
                        <Typography variant="body2">{query.data.support_contact}</Typography>
                      </Stack>
                    )}
                  </Stack>
                </Stack>
              </CardContent>
            </Card>
          </Box>
        )}
      </DataState>
    </Box>
  );
}
