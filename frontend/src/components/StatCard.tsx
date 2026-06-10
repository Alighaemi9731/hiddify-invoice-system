import { Card, CardContent, Typography, Box, Stack } from "@mui/material";
import { alpha } from "@mui/material/styles";
import { ReactNode } from "react";

export default function StatCard({
  label, value, sub, color = "#6d5efc", icon,
}: {
  label: string; value: ReactNode; sub?: ReactNode; color?: string; icon?: ReactNode;
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
      <Box sx={{ position: "absolute", inset: 0, background: (t) =>
        `radial-gradient(120% 120% at 100% 0%, ${alpha(color, t.palette.mode === "dark" ? 0.16 : 0.08)} 0%, transparent 48%)`,
        pointerEvents: "none" }} />
      <CardContent sx={{ p: { xs: 2, sm: 2.5 }, "&:last-child": { pb: { xs: 2, sm: 2.5 } } }}>
        <Stack direction="row" justifyContent="space-between" alignItems="center" spacing={1.5}>
          <Box sx={{ minWidth: 0 }}>
            <Typography variant="body2" color="text.secondary" sx={{ fontWeight: 600 }}>
              {label}
            </Typography>
            <Typography
              component="div"
              sx={{
                mt: 1.4, fontWeight: 850, color: "text.primary", lineHeight: 1.2,
                fontSize: { xs: "1.35rem", sm: "1.65rem" }, whiteSpace: "nowrap",
              }}
            >
              {value}
            </Typography>
            {sub && (
              <Box sx={{ mt: 1, minHeight: 21, color: "text.secondary", fontSize: 12.5 }}>
                {sub}
              </Box>
            )}
          </Box>
          {icon && (
            <Box sx={{
              width: 42, height: 42, borderRadius: "12px", display: "grid", placeItems: "center",
              bgcolor: alpha(color, 0.13), color, flexShrink: 0,
              "& svg": { fontSize: 23 },
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
