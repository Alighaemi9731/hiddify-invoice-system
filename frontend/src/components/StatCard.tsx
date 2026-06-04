import { Card, CardContent, Typography, Box, Stack } from "@mui/material";
import { alpha } from "@mui/material/styles";
import { ReactNode } from "react";

export default function StatCard({
  label, value, sub, color = "#6d5efc", icon,
}: {
  label: string; value: ReactNode; sub?: string; color?: string; icon?: ReactNode;
}) {
  return (
    <Card
      sx={{
        height: "100%", position: "relative", overflow: "hidden",
        transition: "box-shadow .2s, transform .2s, border-color .2s",
        "&:hover": {
          transform: "translateY(-2px)",
          borderColor: (t) => alpha(color, t.palette.mode === "dark" ? 0.5 : 0.35),
          boxShadow: (t) => t.palette.mode === "dark"
            ? `0 8px 26px -10px ${alpha(color, 0.5)}`
            : `0 10px 28px -12px ${alpha(color, 0.45)}`,
        },
      }}
    >
      {/* soft tint wash from the accent corner — gives each card a bit of life */}
      <Box sx={{ position: "absolute", inset: 0, background: (t) =>
        `radial-gradient(120% 120% at 100% 0%, ${alpha(color, t.palette.mode === "dark" ? 0.14 : 0.07)} 0%, transparent 45%)`,
        pointerEvents: "none" }} />
      <Box sx={{ position: "absolute", insetInlineStart: 0, top: 0, bottom: 0, width: 4, bgcolor: color }} />
      <CardContent sx={{ pl: 3 }}>
        <Stack direction="row" justifyContent="space-between" alignItems="flex-start">
          <Box>
            <Typography variant="body2" color="text.secondary">{label}</Typography>
            <Typography variant="h5" sx={{ mt: 0.75, fontWeight: 800, color }}>{value}</Typography>
            {sub && <Typography variant="caption" color="text.secondary">{sub}</Typography>}
          </Box>
          {icon && (
            <Box sx={{
              width: 46, height: 46, borderRadius: "14px", display: "grid", placeItems: "center",
              bgcolor: alpha(color, 0.12), color,
            }}>
              {icon}
            </Box>
          )}
        </Stack>
      </CardContent>
    </Card>
  );
}

export const currentPeriod = () => {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
};
