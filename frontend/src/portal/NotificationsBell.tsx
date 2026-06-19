import { useState } from "react";
import {
  Badge, Box, Divider, IconButton, List, ListItem, ListItemText, Popover, Tooltip, Typography,
} from "@mui/material";
import { alpha, useTheme } from "@mui/material/styles";
import NotificationsNoneIcon from "@mui/icons-material/esm/NotificationsNone";
import { useQuery } from "@tanstack/react-query";
import { portalNotifications, PortalNotification } from "./portalClient";
import { fmtDateTime } from "../format";

const SEEN_KEY = "portal_notif_seen";

const SEV_COLOR: Record<string, "info" | "success" | "warning" | "error"> = {
  info: "info", success: "success", warning: "warning", error: "error",
};

export default function NotificationsBell() {
  const theme = useTheme();
  const [anchor, setAnchor] = useState<HTMLElement | null>(null);
  const [seen, setSeen] = useState<string>(() => localStorage.getItem(SEEN_KEY) || "");

  const { data } = useQuery({
    queryKey: ["portal-notifications"],
    queryFn: portalNotifications,
    refetchInterval: 60000,
    refetchOnWindowFocus: true,
  });
  const events: PortalNotification[] = data?.events || [];
  const unread = events.filter((e) => e.at && e.at > seen).length;

  const open = (el: HTMLElement) => {
    setAnchor(el);
    const now = new Date().toISOString();
    localStorage.setItem(SEEN_KEY, now);
    setSeen(now);
  };

  return (
    <>
      <Tooltip title="اعلان‌ها">
        <IconButton onClick={(e) => open(e.currentTarget)} sx={{ mr: 0.5 }} aria-label="اعلان‌ها">
          <Badge badgeContent={unread} color="error" max={9}>
            <NotificationsNoneIcon />
          </Badge>
        </IconButton>
      </Tooltip>
      <Popover
        open={!!anchor}
        anchorEl={anchor}
        onClose={() => setAnchor(null)}
        anchorOrigin={{ vertical: "bottom", horizontal: "left" }}
        transformOrigin={{ vertical: "top", horizontal: "left" }}
        slotProps={{ paper: { sx: { width: 340, maxWidth: "92vw" } } }}
      >
        <Box sx={{ px: 2, py: 1.5 }}>
          <Typography variant="subtitle2" sx={{ fontWeight: 800 }}>اعلان‌ها</Typography>
        </Box>
        <Divider />
        {events.length === 0 ? (
          <Box sx={{ p: 3, textAlign: "center", color: "text.secondary" }}>
            <Typography variant="body2">اعلانی نیست.</Typography>
          </Box>
        ) : (
          <List dense sx={{ maxHeight: 380, overflowY: "auto", py: 0 }}>
            {events.map((e) => {
              const color = theme.palette[SEV_COLOR[e.severity] || "info"].main;
              return (
                <ListItem key={e.key} sx={{ borderInlineStart: `3px solid ${color}`, bgcolor: alpha(color, 0.05) }}>
                  <ListItemText
                    primary={<Typography variant="body2">{e.title}</Typography>}
                    secondary={e.at ? <span dir="ltr">{fmtDateTime(e.at)}</span> : undefined}
                  />
                </ListItem>
              );
            })}
          </List>
        )}
      </Popover>
    </>
  );
}
