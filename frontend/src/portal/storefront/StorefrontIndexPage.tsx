import { useEffect } from "react";
import { Alert, Box, Button, Card, CardActionArea, CardContent, Grid, Stack, Typography } from "@mui/material";
import StorefrontIcon from "@mui/icons-material/esm/Storefront";
import ArrowBackIcon from "@mui/icons-material/esm/ArrowBack";
import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { DataState } from "../../components/DataState";
import { listStorefronts, storefrontQueryKeys } from "./api";

export default function StorefrontIndexPage() {
  const navigate = useNavigate();
  const query = useQuery({ queryKey: storefrontQueryKeys.all, queryFn: listStorefronts });

  useEffect(() => {
    if (query.data?.length === 1) {
      navigate(`/portal/storefront/${query.data[0].id}`, { replace: true });
    }
  }, [navigate, query.data]);

  return (
    <Box>
      <Stack spacing={0.5} sx={{ mb: 2.5 }}>
        <Typography variant="h5">فروشگاه من</Typography>
        <Typography variant="body2" color="text.secondary">
          فروشگاهی را که می‌خواهید بررسی کنید انتخاب کنید.
        </Typography>
      </Stack>

      <DataState
        isLoading={query.isLoading}
        isError={query.isError}
        rows={3}
        onRetry={() => query.refetch()}
      >
        {!query.data?.length ? (
          <Alert severity="info" icon={<StorefrontIcon />}>
            هنوز فروشگاهی برای این حساب فعال نشده است. راه‌اندازی فروشگاه از طریق ربات انجام می‌شود.
          </Alert>
        ) : query.data.length > 1 ? (
          <Grid container spacing={2}>
            {query.data.map((shop) => (
              <Grid item xs={12} md={6} lg={4} key={shop.id}>
                <Card sx={{ height: "100%" }}>
                  <CardActionArea
                    onClick={() => navigate(`/portal/storefront/${shop.id}`)}
                    sx={{ height: "100%" }}
                    aria-label={`باز کردن فروشگاه ${shop.reseller.name}`}
                  >
                    <CardContent>
                      <Stack direction="row" alignItems="center" spacing={1.5}>
                        <StorefrontIcon color="primary" />
                        <Box sx={{ flexGrow: 1, minWidth: 0 }}>
                          <Typography sx={{ fontWeight: 800 }} noWrap>{shop.reseller.name}</Typography>
                          <Typography variant="caption" color="text.secondary">
                            {shop.bot_username ? `@${shop.bot_username.replace(/^@/, "")}` : shop.panel.key}
                          </Typography>
                        </Box>
                        <ArrowBackIcon color="action" />
                      </Stack>
                    </CardContent>
                  </CardActionArea>
                </Card>
              </Grid>
            ))}
          </Grid>
        ) : (
          <Button onClick={() => navigate(`/portal/storefront/${query.data[0].id}`)}>
            ورود به فروشگاه
          </Button>
        )}
      </DataState>
    </Box>
  );
}
