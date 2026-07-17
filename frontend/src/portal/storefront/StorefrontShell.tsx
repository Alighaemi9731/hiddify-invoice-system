import { Alert, Box, Button, MenuItem, Select, Stack, Tab, Tabs, Typography } from "@mui/material";
import { useQuery } from "@tanstack/react-query";
import { Navigate, Outlet, useLocation, useNavigate, useParams } from "react-router-dom";
import { DataState } from "../../components/DataState";
import { fmtNum } from "../../format";
import { currentTehranMonthRange, getStorefrontDashboard, listStorefronts, storefrontQueryKeys } from "./api";
import type { StorefrontShop } from "./types";

export type StorefrontOutletContext = { shop: StorefrontShop };

export default function StorefrontShell() {
  const { shopId: rawShopId } = useParams();
  const shopId = Number(rawShopId);
  const navigate = useNavigate();
  const location = useLocation();
  const query = useQuery({ queryKey: storefrontQueryKeys.all, queryFn: listStorefronts });
  const shop = query.data?.find((item) => item.id === shopId);
  const activeTab = location.pathname.endsWith("/health") ? "health"
    : location.pathname.endsWith("/plans") ? "plans"
      : location.pathname.endsWith("/settings") ? "settings"
        : location.pathname.endsWith("/managers") ? "managers"
          : location.pathname.endsWith("/preview") ? "preview"
            : location.pathname.endsWith("/topups") ? "topups"
              : location.pathname.includes("/customers") ? "customers"
                : "dashboard";

  // Pending top-up count drives the «شارژها» tab badge (shared cache with the dashboard + top-ups pages).
  const range = currentTehranMonthRange();
  const pendingQuery = useQuery({
    queryKey: storefrontQueryKeys.dashboard(shopId, range.from, range.to),
    queryFn: () => getStorefrontDashboard(shopId, range.from, range.to),
    enabled: Number.isSafeInteger(shopId) && shopId > 0 && !!shop,
    select: (data) => data.pending_topups.count,
  });
  const pendingTopups = pendingQuery.data ?? 0;

  if (!Number.isSafeInteger(shopId) || shopId <= 0) {
    return <Navigate to="/portal/storefront" replace />;
  }

  return (
    <DataState
      isLoading={query.isLoading}
      isError={query.isError}
      rows={4}
      onRetry={() => query.refetch()}
    >
      {!shop ? (
        <Alert
          severity="warning"
          action={<Button color="inherit" onClick={() => navigate("/portal/storefront")}>بازگشت</Button>}
        >
          این فروشگاه پیدا نشد یا به حساب شما تعلق ندارد.
        </Alert>
      ) : (
        <Box>
          <Stack
            direction={{ xs: "column", sm: "row" }}
            alignItems={{ xs: "stretch", sm: "center" }}
            justifyContent="space-between"
            spacing={1.5}
            sx={{ mb: 2 }}
          >
            <Box>
              <Typography variant="h5">{shop.reseller.name}</Typography>
              <Typography variant="body2" color="text.secondary" sx={{ mt: 0.4 }}>
                {shop.bot_username ? `@${shop.bot_username.replace(/^@/, "")}` : "ربات فروشگاه"}
                {shop.panel.key ? ` · پنل ${shop.panel.key}` : ""}
              </Typography>
            </Box>
            {query.data && query.data.length > 1 && (
              <Select
                size="small"
                value={shop.id}
                onChange={(event) => navigate(`/portal/storefront/${event.target.value}`)}
                inputProps={{ "aria-label": "انتخاب فروشگاه" }}
                sx={{ minWidth: { xs: "100%", sm: 220 } }}
              >
                {query.data.map((option) => (
                  <MenuItem key={option.id} value={option.id}>{option.reseller.name}</MenuItem>
                ))}
              </Select>
            )}
          </Stack>

          {(!shop.enabled || shop.shop_closed || shop.health_error_class) && (
            <Alert severity={shop.health_error_class ? "warning" : "info"} sx={{ mb: 2 }}>
              {shop.health_error_class
                ? "در آخرین بررسی، برای ربات یا پنل فروشگاه خطایی ثبت شده است. جزئیات را در بخش سلامت ببینید."
                : shop.shop_closed
                  ? "فروشگاه در حال حاضر بسته است."
                  : "ربات فروشگاه در حال حاضر غیرفعال است."}
            </Alert>
          )}

          <Tabs
            value={activeTab}
            onChange={(_, value) => navigate(value === "dashboard"
              ? `/portal/storefront/${shop.id}`
              : `/portal/storefront/${shop.id}/${value}`)}
            variant="scrollable"
            scrollButtons="auto"
            sx={{ mb: 2, borderBottom: 1, borderColor: "divider" }}
          >
            <Tab value="dashboard" label="داشبورد فروش" />
            <Tab value="plans" label="پلن‌ها" />
            <Tab value="customers" label="مشتریان" />
            <Tab value="topups" label={<ShellTabBadge text="شارژها" count={pendingTopups} />} />
            <Tab value="settings" label="تنظیمات" />
            <Tab value="managers" label="مدیران" />
            <Tab value="preview" label="پیش‌نمایش مشتری" />
            <Tab value="health" label="سلامت فروشگاه" />
          </Tabs>

          <Outlet context={{ shop } satisfies StorefrontOutletContext} />
        </Box>
      )}
    </DataState>
  );
}

function ShellTabBadge({ text, count }: { text: string; count: number }) {
  return (
    <Stack direction="row" spacing={0.75} alignItems="center">
      <span>{text}</span>
      {count > 0 && (
        <Box
          component="span"
          sx={{
            bgcolor: "error.main", color: "error.contrastText", borderRadius: 999,
            px: 0.9, minWidth: 20, textAlign: "center", fontSize: 12, fontWeight: 800, lineHeight: "20px",
          }}
        >
          {fmtNum(count)}
        </Box>
      )}
    </Stack>
  );
}
