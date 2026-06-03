import { Box, LinearProgress, Tooltip, Typography } from "@mui/material";

/**
 * A small "how full is this admin's user quota" meter.
 * Blue = plenty of room, amber = filling up (≥70%), red = nearly full (≥90%).
 * `used`/`max` are user counts; max=0/null means "no limit" → shown as ∞.
 */
export default function CapacityBar({ used, max }: { used: number; max?: number | null }) {
  const hasLimit = !!max && max > 0;
  const pct = hasLimit ? Math.min(100, Math.round((used / (max as number)) * 100)) : 0;
  const color: "info" | "warning" | "error" =
    pct >= 90 ? "error" : pct >= 70 ? "warning" : "info";

  const label = hasLimit ? `${used}/${max}` : `${used}/∞`;
  return (
    <Tooltip title={hasLimit ? `${pct}% پر شده` : "بدون سقف"}>
      <Box sx={{ minWidth: 92 }}>
        <Box sx={{ display: "flex", justifyContent: "space-between", mb: 0.25 }}>
          <Typography variant="caption" color="text.secondary" dir="ltr">{label}</Typography>
          {hasLimit && <Typography variant="caption" color={`${color}.main`}>{pct}%</Typography>}
        </Box>
        <LinearProgress
          variant="determinate" value={hasLimit ? pct : 0} color={color}
          sx={{ height: 6, borderRadius: 3, opacity: hasLimit ? 1 : 0.3 }}
        />
      </Box>
    </Tooltip>
  );
}
