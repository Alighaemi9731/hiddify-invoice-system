import { ReactNode } from "react";
import { Box, Card, CardContent, Typography } from "@mui/material";

export function SectionCard({
  title, action, children,
}: { title: string; action?: ReactNode; children: ReactNode }) {
  return (
    <Card sx={{ height: "100%", overflow: "hidden" }}>
      <Box
        sx={{
          px: { xs: 2, sm: 2.5 }, py: 1.8, borderBottom: 1, borderColor: "divider",
          display: "flex", alignItems: "center", justifyContent: "space-between", gap: 1.5,
        }}
      >
        <Typography variant="subtitle1" sx={{ fontWeight: 800 }}>{title}</Typography>
        {action}
      </Box>
      <CardContent sx={{ p: { xs: 2, sm: 2.5 }, "&:last-child": { pb: { xs: 2, sm: 2.5 } } }}>
        {children}
      </CardContent>
    </Card>
  );
}

export function EmptyState({ children }: { children: ReactNode }) {
  return (
    <Box sx={{ minHeight: 180, display: "grid", placeItems: "center", color: "text.secondary", textAlign: "center" }}>
      <Typography variant="body2">{children}</Typography>
    </Box>
  );
}
