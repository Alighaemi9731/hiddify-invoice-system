import { Card, CardContent, Typography, Box, Stack } from "@mui/material";
import { alpha } from "@mui/material/styles";
import { ReactNode } from "react";

export default function StatCard({
  label, value, sub, color = "#1f3b73", icon,
}: {
  label: string; value: ReactNode; sub?: string; color?: string; icon?: ReactNode;
}) {
  return (
    <Card
      sx={{
        height: "100%", position: "relative", overflow: "hidden",
        boxShadow: "0 1px 3px rgba(16,24,40,.06), 0 1px 2px rgba(16,24,40,.04)",
        transition: "box-shadow .2s, transform .2s",
        "&:hover": { boxShadow: "0 6px 20px rgba(16,24,40,.10)", transform: "translateY(-2px)" },
      }}
    >
      <Box sx={{ position: "absolute", insetInlineStart: 0, top: 0, bottom: 0, width: 5, bgcolor: color }} />
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
