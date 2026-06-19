import { Box, Card, CardContent, Grid, IconButton, Stack, Tooltip, Typography } from "@mui/material";
import ContentCopyIcon from "@mui/icons-material/esm/ContentCopy";
import OpenInNewIcon from "@mui/icons-material/esm/OpenInNew";
import DnsIcon from "@mui/icons-material/esm/Dns";
import { useQuery } from "@tanstack/react-query";
import { portalPanels } from "../portalClient";
import { DataState } from "../../components/DataState";
import { useToast } from "../../components/Toast";
import { EmptyState } from "../ui";

export default function PortalPanels() {
  const { data = [], isLoading, isError, refetch } = useQuery({
    queryKey: ["portal-panels"],
    queryFn: portalPanels,
  });
  const { node: toast, show } = useToast();

  const copy = async (link: string) => {
    try {
      await navigator.clipboard.writeText(link);
      show("لینک کپی شد", "success");
    } catch {
      show("کپی نشد؛ لینک را دستی انتخاب کنید", "error");
    }
  };

  return (
    <Box>
      {toast}
      <Typography variant="h5" sx={{ mb: 0.4 }}>پنل‌های من</Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 2.5 }}>
        لینکِ مدیریتِ شما روی هر پنل — برای کپی روی دکمه بزنید
      </Typography>

      <DataState isLoading={isLoading} isError={isError} onRetry={refetch} rows={3}>
        {data.length === 0 ? (
          <Card><EmptyState>پنلی برای شما ثبت نشده است.</EmptyState></Card>
        ) : (
          <Grid container spacing={2}>
            {data.map((p) => (
              <Grid item xs={12} md={6} key={p.reseller_id}>
                <Card sx={{ height: "100%" }}>
                  <CardContent>
                    <Stack direction="row" alignItems="center" spacing={1.2} sx={{ mb: 1.5 }}>
                      <Box sx={{ width: 36, height: 36, borderRadius: 2, display: "grid", placeItems: "center", bgcolor: "action.hover", color: "primary.main" }}>
                        <DnsIcon fontSize="small" />
                      </Box>
                      <Box sx={{ minWidth: 0 }}>
                        <Typography variant="subtitle1" noWrap sx={{ fontWeight: 800 }}>{p.panel_name}</Typography>
                        <Typography variant="caption" color="text.secondary">{p.name}</Typography>
                      </Box>
                    </Stack>

                    <Box
                      dir="ltr"
                      sx={{
                        p: 1.2, borderRadius: 2, bgcolor: "action.hover",
                        fontFamily: "monospace", fontSize: 12.5, wordBreak: "break-all",
                        display: "flex", alignItems: "center", gap: 1,
                      }}
                    >
                      <Box sx={{ flex: 1, minWidth: 0 }}>{p.link}</Box>
                      <Tooltip title="کپی لینک">
                        <IconButton size="small" onClick={() => copy(p.link)} aria-label="کپی لینک پنل">
                          <ContentCopyIcon sx={{ fontSize: 17 }} />
                        </IconButton>
                      </Tooltip>
                      <Tooltip title="باز کردن">
                        <IconButton size="small" component="a" href={p.link} target="_blank" rel="noopener noreferrer" aria-label="باز کردن لینک پنل">
                          <OpenInNewIcon sx={{ fontSize: 17 }} />
                        </IconButton>
                      </Tooltip>
                    </Box>
                  </CardContent>
                </Card>
              </Grid>
            ))}
          </Grid>
        )}
      </DataState>
    </Box>
  );
}
