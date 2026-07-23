import { useState, Suspense } from "react";
import { Outlet, useLocation, useNavigate } from "react-router-dom";
import {
  AppBar, Box, Chip, Drawer, IconButton, List, ListItemButton, ListItemIcon,
  ListItemText, Stack, Toolbar, Typography, Divider, Tooltip, useMediaQuery,
  CircularProgress,
} from "@mui/material";
import { alpha, useTheme } from "@mui/material/styles";
import DashboardIcon from "@mui/icons-material/esm/Dashboard";
import ReceiptLongIcon from "@mui/icons-material/esm/ReceiptLong";
import PaymentsIcon from "@mui/icons-material/esm/Payments";
import GroupIcon from "@mui/icons-material/esm/Group";
import DnsIcon from "@mui/icons-material/esm/Dns";
import SupportAgentIcon from "@mui/icons-material/esm/SupportAgent";
import HelpOutlineIcon from "@mui/icons-material/esm/HelpOutline";
import StorefrontIcon from "@mui/icons-material/esm/Storefront";
import LogoutIcon from "@mui/icons-material/esm/Logout";
import MenuIcon from "@mui/icons-material/esm/Menu";
import PersonOutlineIcon from "@mui/icons-material/esm/PersonOutline";
import DarkModeIcon from "@mui/icons-material/esm/DarkModeOutlined";
import LightModeIcon from "@mui/icons-material/esm/LightModeOutlined";
import { usePortalAuth } from "./PortalAuthContext";
import { useColorMode } from "../colorMode";
import {
  CHROME_BLUR,
  CHROME_SIDEBAR_BG,
  CHROME_SIDEBAR_BORDER,
  GLOSS_NAV_CHIP,
  GLOSS_NAV_SELECTED,
  SIDEBAR_AMBIENT,
  SIDEBAR_WIDTH,
  TIER2_BLUR,
} from "../themeTokens";
import ErrorBoundary from "../components/ErrorBoundary";
import { PageTransition } from "../components/motion";
import NotificationsBell from "./NotificationsBell";
import { useQuery } from "@tanstack/react-query";
import { listStorefronts, storefrontQueryKeys } from "./storefront/api";

const WIDTH = SIDEBAR_WIDTH;

const NAV = [
  { to: "/portal", label: "داشبورد", icon: <DashboardIcon />, color: "#0071e3" },
  { to: "/portal/invoices", label: "فاکتورها", icon: <ReceiptLongIcon />, color: "#f59e0b" },
  { to: "/portal/payments", label: "پرداخت‌ها", icon: <PaymentsIcon />, color: "#10b981" },
  { to: "/portal/subs", label: "زیرمجموعه‌ها", icon: <GroupIcon />, color: "#22c55e" },
  { to: "/portal/panels", label: "پنل‌ها و ظرفیت", icon: <DnsIcon />, color: "#0ea5e9" },
  { to: "/portal/storefront", label: "فروشگاه من", icon: <StorefrontIcon />, color: "#ec4899", storefront: true },
  { to: "/portal/support", label: "پشتیبانی", icon: <SupportAgentIcon />, color: "#a855f7" },
  { to: "/portal/help", label: "راهنما", icon: <HelpOutlineIcon />, color: "#06b6d4" },
];

export default function PortalLayout() {
  const theme = useTheme();
  const isDark = theme.palette.mode === "dark";
  const isDesktop = useMediaQuery(theme.breakpoints.up("md"));
  const [open, setOpen] = useState(false);
  const nav = useNavigate();
  const loc = useLocation();
  const { resellers, logout } = usePortalAuth();
  const { mode, toggle } = useColorMode();
  const primary = theme.palette.primary.main;
  const storefronts = useQuery({ queryKey: storefrontQueryKeys.all, queryFn: listStorefronts });
  const visibleNav = NAV.filter((item) => !item.storefront || !!storefronts.data?.length);
  const isSelected = (to: string) => to === "/portal"
    ? loc.pathname === to
    : loc.pathname === to || loc.pathname.startsWith(`${to}/`);

  const title = resellers[0]?.name || "نماینده";

  const navItemSx = (selected: boolean) => ({
    position: "relative",
    borderRadius: 2.5,
    mx: 1.25,
    my: 0.3,
    py: 0.7,
    color: selected ? "text.primary" : "text.secondary",
    backdropFilter: selected ? TIER2_BLUR : "none",
    WebkitBackdropFilter: selected ? TIER2_BLUR : "none",
    "& .MuiListItemIcon-root": { minWidth: 40 },
    "&.Mui-selected": {
      backgroundColor: isDark ? "rgba(255,255,255,.07)" : "rgba(255,255,255,.60)",
      backgroundImage: GLOSS_NAV_SELECTED,
      boxShadow: isDark
        ? "inset 0 1px 0 rgba(255,255,255,.14), 0 2px 12px -6px rgba(0,0,0,.45)"
        : "inset 0 1px 0 rgba(255,255,255,.96), 0 2px 12px -6px rgba(0,0,0,.18)",
      border: isDark ? "1px solid rgba(255,255,255,.10)" : "1px solid rgba(255,255,255,.78)",
      "&:hover": {
        backgroundColor: isDark ? "rgba(255,255,255,.10)" : "rgba(255,255,255,.76)",
      },
      "&::before": {
        content: '""',
        position: "absolute",
        insetInlineStart: 4,
        top: 9,
        bottom: 9,
        width: 3,
        borderRadius: 3,
        background: `linear-gradient(180deg, ${alpha(primary, 0.9)} 0%, ${alpha(primary, 0.55)} 100%)`,
        boxShadow: `0 0 8px 2px ${alpha(primary, 0.42)}`,
      },
    },
    "&:hover:not(.Mui-selected)": {
      backgroundColor: isDark ? "rgba(255,255,255,.05)" : "rgba(255,255,255,.38)",
    },
    "&:hover .nav-icon": { transform: "scale(1.14) rotate(-3deg)" },
    transition: "background-color .18s, box-shadow .18s, border .18s",
  });

  const sidebar = (
    <Box sx={{ height: "100%", display: "flex", flexDirection: "column", position: "relative", overflow: "hidden" }}>
      <Box
        aria-hidden
        sx={{
          position: "absolute",
          inset: 0,
          background: isDark ? SIDEBAR_AMBIENT.dark : SIDEBAR_AMBIENT.light,
          pointerEvents: "none",
          zIndex: 0,
        }}
      />
      <Box sx={{ position: "relative", zIndex: 1, display: "flex", flexDirection: "column", height: "100%" }}>
        <Toolbar sx={{ py: 2.5 }}>
          <Stack direction="row" alignItems="center" spacing={1.25}>
            <Box
              sx={{
                width: 40, height: 40, borderRadius: 2.5, color: "#fff",
                display: "grid", placeItems: "center",
                background: "linear-gradient(145deg, #5ab5ff 0%, #0071e3 100%)",
                boxShadow: "0 6px 18px -6px rgba(0,113,227,.55), inset 0 1.5px 0 rgba(255,255,255,.40)",
              }}
            >
              <ReceiptLongIcon fontSize="small" />
            </Box>
            <Box>
              <Typography variant="subtitle1" sx={{ fontWeight: 800, lineHeight: 1.1 }}>
                پنلِ نماینده
              </Typography>
              <Typography variant="caption" color="text.secondary">
                مدیریت فروش و فاکتورها
              </Typography>
            </Box>
          </Stack>
        </Toolbar>

        <Divider />

        <List sx={{ py: 1, flexGrow: 1 }}>
          {visibleNav.map((item) => {
            const selected = isSelected(item.to);
            return (
              <ListItemButton
                key={item.to}
                selected={selected}
                onClick={() => { nav(item.to); setOpen(false); }}
                sx={navItemSx(selected)}
              >
                <ListItemIcon>
                  <Box
                    className="nav-icon"
                    sx={{
                      width: 31, height: 31, borderRadius: 2,
                      display: "grid", placeItems: "center",
                      color: item.color,
                      bgcolor: alpha(item.color, isDark ? 0.22 : 0.14),
                      backgroundImage: GLOSS_NAV_CHIP,
                      boxShadow: selected
                        ? `0 0 0 1px ${alpha(item.color, 0.5)}, 0 4px 12px -4px ${alpha(item.color, 0.55)}, inset 0 1px 0 rgba(255,255,255,.32)`
                        : "inset 0 1px 0 rgba(255,255,255,.18)",
                      transition: "transform .2s cubic-bezier(.34,1.56,.64,1), box-shadow .15s",
                      "& svg": { fontSize: 19 },
                    }}
                  >
                    {item.icon}
                  </Box>
                </ListItemIcon>
                <ListItemText
                  primary={item.label}
                  primaryTypographyProps={{ fontWeight: selected ? 700 : 500, fontSize: 14.5 }}
                />
              </ListItemButton>
            );
          })}

          <Divider sx={{ mx: 1.5, my: 1 }} />

          <ListItemButton
            onClick={() => { logout(); nav("/portal/login", { replace: true }); }}
            sx={{
              borderRadius: 2.5, mx: 1.25, my: 0.3, py: 0.85,
              color: "error.main",
              "& .MuiListItemIcon-root": { color: "error.main", minWidth: 38 },
              "&:hover": { bgcolor: alpha(theme.palette.error.main, 0.08) },
            }}
          >
            <ListItemIcon><LogoutIcon /></ListItemIcon>
            <ListItemText primary="خروج" primaryTypographyProps={{ fontWeight: 600, fontSize: 14.5 }} />
          </ListItemButton>
        </List>
      </Box>
    </Box>
  );

  const sidebarGlassSx = {
    backdropFilter: CHROME_BLUR,
    WebkitBackdropFilter: CHROME_BLUR,
    backgroundColor: isDark ? CHROME_SIDEBAR_BG.dark : CHROME_SIDEBAR_BG.light,
    borderInlineStart: `1px solid ${isDark ? CHROME_SIDEBAR_BORDER.dark : CHROME_SIDEBAR_BORDER.light}`,
  };

  return (
    <Box sx={{ display: "flex", minHeight: "100vh" }}>
      {isDesktop ? (
        <Box
          component="nav"
          sx={{
            width: WIDTH, flexShrink: 0, position: "sticky", top: 0,
            alignSelf: "flex-start", height: "100vh", overflowY: "auto", ...sidebarGlassSx,
          }}
        >
          {sidebar}
        </Box>
      ) : (
        <Drawer
          variant="temporary"
          anchor="right"
          open={open}
          onClose={() => setOpen(false)}
          ModalProps={{ keepMounted: true }}
          sx={{ "& .MuiDrawer-paper": { width: WIDTH, boxSizing: "border-box" } }}
        >
          {sidebar}
        </Drawer>
      )}

      <Box component="main" sx={{ flexGrow: 1, minWidth: 0, display: "flex", flexDirection: "column" }}>
        <AppBar position="sticky" elevation={0} color="transparent"
          sx={{ borderBottom: "1px solid", borderColor: "divider", pt: "env(safe-area-inset-top)" }}>
          <Toolbar>
            {!isDesktop && (
              <IconButton edge="start" onClick={() => setOpen(true)} sx={{ ml: 1 }} aria-label="باز کردن منو">
                <MenuIcon />
              </IconButton>
            )}
            <Typography variant="h6" sx={{ flexGrow: 1, fontWeight: 800 }}>
              {visibleNav.find((item) => isSelected(item.to))?.label || ""}
            </Typography>
            <NotificationsBell />
            <Tooltip title={mode === "dark" ? "حالت روشن" : "حالت تیره"}>
              <IconButton onClick={toggle} sx={{ mr: 1 }}>
                {mode === "dark" ? <LightModeIcon /> : <DarkModeIcon />}
              </IconButton>
            </Tooltip>
            <Chip icon={<PersonOutlineIcon />} label={title} variant="outlined" size="small" />
          </Toolbar>
        </AppBar>

        <Box sx={{ p: { xs: 2, md: 3 }, flexGrow: 1 }}>
          <ErrorBoundary>
            <Suspense
              fallback={
                <Box sx={{ display: "grid", placeItems: "center", py: 12 }}>
                  <CircularProgress />
                </Box>
              }
            >
              <PageTransition>
                <Outlet />
              </PageTransition>
            </Suspense>
          </ErrorBoundary>
        </Box>
      </Box>
    </Box>
  );
}
