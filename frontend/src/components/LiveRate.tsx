import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Box, Card, CardContent, Chip, IconButton, Stack, Tooltip, Typography,
} from "@mui/material";
import { alpha } from "@mui/material/styles";
import RefreshIcon from "@mui/icons-material/esm/Refresh";
import CurrencyExchangeIcon from "@mui/icons-material/esm/CurrencyExchange";
import { getRates, refreshRate } from "../api/client";
import { fmtNum, fmtDateTime } from "../format";
import { useToast } from "./Toast";

/**
 * Live exchange-rate widget. `compact` renders a single chip (for the Invoices header);
 * otherwise a full card (for the Dashboard). The displayed value auto-refreshes every 60s
 * (reads the cached server value — no external call); the refresh button forces a live fetch.
 */
export default function LiveRate({ compact = false }: { compact?: boolean }) {
  const qc = useQueryClient();
  const { node, show } = useToast();
  const { data } = useQuery({ queryKey: ["rates"], queryFn: getRates, refetchInterval: 60_000 });

  const refresh = useMutation({
    mutationFn: refreshRate,
    onSuccess: (r: any) => {
      show(`نرخ آنلاین به‌روزرسانی شد: ${fmtNum(Number(r?.rate || 0))} تومان`);
      qc.invalidateQueries({ queryKey: ["rates"] });
    },
    onError: () => show("دریافت نرخ آنلاین ناموفق بود؛ بعداً دوباره تلاش کنید.", "error"),
  });

  const eff = Number(data?.effective || 0);

  if (compact) {
    return (
      <Tooltip title="نرخی که هنگام صدور فاکتور اعمال می‌شود">
        <Chip
          size="small"
          icon={<CurrencyExchangeIcon fontSize="small" />}
          label={`نرخ اعمالی: ${eff ? fmtNum(eff) : "—"} ت`}
          variant="outlined"
          color={data?.stale ? "warning" : "default"}
        />
      </Tooltip>
    );
  }

  const modeChip = data?.stale
    ? { label: "کهنه — نرخ دستی اعمال می‌شود", color: "warning" as const }
    : data?.mode === "auto"
      ? { label: "خودکار (آنلاین)", color: "success" as const }
      : { label: "دستی", color: "default" as const };

  return (
    <Card sx={{ height: "100%" }}>
      {node}
      <CardContent>
        <Stack direction="row" alignItems="center" justifyContent="space-between" sx={{ mb: 1 }}>
          <Stack direction="row" spacing={1} alignItems="center">
            <Box sx={{ display: "flex", color: "primary.main",
              p: 0.8, borderRadius: 2, bgcolor: (t) => alpha(t.palette.primary.main, 0.1) }}>
              <CurrencyExchangeIcon fontSize="small" />
            </Box>
            <Typography sx={{ fontWeight: 800 }}>نرخِ زنده</Typography>
          </Stack>
          <Tooltip title="به‌روزرسانی نرخ آنلاین">
            <span>
              <IconButton size="small" onClick={() => refresh.mutate()} disabled={refresh.isPending}>
                <RefreshIcon fontSize="small" />
              </IconButton>
            </span>
          </Tooltip>
        </Stack>

        <Stack spacing={1.1}>
          <Stack direction="row" alignItems="baseline" justifyContent="space-between">
            <Typography variant="body2" color="text.secondary">دلار / تتر (USDT)</Typography>
            <Typography sx={{ fontWeight: 750 }}>
              {eff ? fmtNum(eff) : "—"}
              <Typography component="span" variant="caption" color="text.secondary"> تومان</Typography>
            </Typography>
          </Stack>

          {data?.ton_enabled && (
            <Stack direction="row" alignItems="baseline" justifyContent="space-between">
              <Typography variant="body2" color="text.secondary">تون‌کوین (TON)</Typography>
              <Typography sx={{ fontWeight: 750 }}>
                {data?.ton_auto ? fmtNum(Number(data.ton_auto)) : "—"}
                <Typography component="span" variant="caption" color="text.secondary"> تومان</Typography>
              </Typography>
            </Stack>
          )}

          <Stack direction="row" alignItems="center" spacing={1} sx={{ flexWrap: "wrap" }}>
            <Chip size="small" label={modeChip.label} color={modeChip.color} variant="outlined" />
            {data?.mode === "auto" && data?.usdt_auto_at && (
              <Typography variant="caption" color="text.secondary" dir="ltr">
                {fmtDateTime(data.usdt_auto_at)}
              </Typography>
            )}
          </Stack>
        </Stack>
      </CardContent>
    </Card>
  );
}
