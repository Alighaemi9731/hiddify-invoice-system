import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Box, IconButton, Stack, Tooltip, Typography } from "@mui/material";
import { alpha } from "@mui/material/styles";
import RefreshIcon from "@mui/icons-material/esm/Refresh";
import CurrencyExchangeIcon from "@mui/icons-material/esm/CurrencyExchange";
import { getRates, refreshRate } from "../api/client";
import { fmtNum } from "../format";
import { useToast } from "./Toast";

/**
 * Inline live exchange-rate widget for the Invoices header: shows the applied USDT (Tether)
 * AND TON rates with a refresh button that fetches both live. The display auto-refreshes every
 * 60s from the cached server value (no external call); the button forces a live fetch.
 */
export default function LiveRate() {
  const qc = useQueryClient();
  const { node, show } = useToast();
  const { data } = useQuery({ queryKey: ["rates"], queryFn: getRates, refetchInterval: 60_000 });

  const refresh = useMutation({
    mutationFn: refreshRate,
    onSuccess: () => {
      show("نرخ‌های آنلاین به‌روزرسانی شد.");
      qc.invalidateQueries({ queryKey: ["rates"] });
    },
    onError: () => show("دریافت نرخ آنلاین ناموفق بود؛ بعداً دوباره تلاش کنید.", "error"),
  });

  const usdt = Number(data?.effective || 0);
  const ton = Number(data?.ton_effective || 0);

  return (
    <Box
      sx={{
        display: "flex",
        alignItems: "center",
        gap: 1,
        px: 1.25,
        py: 0.5,
        borderRadius: "50px",
        border: 1,
        borderColor: data?.stale ? "warning.main" : "divider",
        bgcolor: (t) => alpha(t.palette.primary.main, 0.05),
      }}
    >
      {node}
      <CurrencyExchangeIcon fontSize="small" sx={{ color: "primary.main" }} />
      <Stack direction="row" spacing={1.25} alignItems="center" sx={{ flexWrap: "wrap" }}>
        <Tooltip title={`نرخی که هنگام صدور فاکتور اعمال می‌شود${data?.stale ? " (نرخ آنلاین کهنه است؛ نرخ دستی اعمال می‌شود)" : ""}`}>
          <Typography variant="body2" sx={{ fontWeight: 700, whiteSpace: "nowrap" }}>
            تتر: {usdt ? fmtNum(usdt) : "—"}
            <Typography component="span" variant="caption" color="text.secondary"> ت</Typography>
          </Typography>
        </Tooltip>
        <Box sx={{ width: "1px", height: 16, bgcolor: "divider" }} />
        <Typography variant="body2" sx={{ fontWeight: 700, whiteSpace: "nowrap" }}>
          تون: {ton ? fmtNum(ton) : "—"}
          <Typography component="span" variant="caption" color="text.secondary"> ت</Typography>
        </Typography>
      </Stack>
      <Tooltip title="به‌روزرسانی نرخ‌ها (تتر و تون)">
        <span>
          <IconButton size="small" onClick={() => refresh.mutate()} disabled={refresh.isPending}>
            <RefreshIcon fontSize="small" />
          </IconButton>
        </span>
      </Tooltip>
    </Box>
  );
}
