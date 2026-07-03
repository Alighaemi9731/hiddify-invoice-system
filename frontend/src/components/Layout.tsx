import { useState, Suspense } from "react";
import { Outlet, useLocation, useNavigate } from "react-router-dom";
import {
  AppBar, Box, Chip, Drawer, IconButton, List, ListItemButton, ListItemIcon,
  ListItemText, Stack, Toolbar, Typography, Divider, Tooltip, useMediaQuery,
  CircularProgress,
} from "@mui/material";
import { alpha, useTheme } from "@mui/material/styles";
import DashboardIcon from "@mui/icons-material/esm/Dashboard";
import DnsIcon from "@mui/icons-material/esm/Dns";
import GroupIcon from "@mui/icons-material/esm/Group";
import ReceiptLongIcon from "@mui/icons-material/esm/ReceiptLong";
import PaymentsIcon from "@mui/icons-material/esm/Payments";
import MoneyOffIcon from "@mui/icons-material/esm/MoneyOff";
import BarChartIcon from "@mui/icons-material/esm/BarChart";
import AccountBalanceIcon from "@mui/icons-material/esm/AccountBalance";
import HistoryIcon from "@mui/icons-material/esm/History";
import CampaignIcon from "@mui/icons-material/esm/Campaign";
import ManageAccountsIcon from "@mui/icons-material/esm/ManageAccounts";
import HelpOutlineIcon from "@mui/icons-material/esm/HelpOutline";
import BuildIcon from "@mui/icons-material/esm/Build";
import SettingsIcon from "@mui/icons-material/esm/Settings";
import LogoutIcon from "@mui/icons-material/esm/Logout";
import MenuIcon from "@mui/icons-material/esm/Menu";
import PersonOutlineIcon from "@mui/icons-material/esm/PersonOutline";
import DarkModeIcon from "@mui/icons-material/esm/DarkModeOutlined";
import LightModeIcon from "@mui/icons-material/esm/LightModeOutlined";
import { useQuery } from "@tanstack/react-query";
import { useAuth } from "../auth/AuthContext";
import { useColorMode } from "../colorMode";
import { getInfo } from "../api/client";
import ErrorBoundary from "./ErrorBoundary";
import BottomNav from "./BottomNav";
import { PageTransition } from "./motion";
import { useResponsiveTableLabels } from "../responsive";

const WIDTH = 256;

const NAV = [
  { to: "/", label: "داشبورد", icon: <DashboardIcon />, color: "#0071e3" },
  { to: "/panels", label: "پنل‌ها", icon: <DnsIcon />, color: "#0ea5e9" },
  { to: "/resellers", label: "نمایندگان", icon: <GroupIcon />, color: "#22c55e" },
  { to: "/invoices", label: "فاکتورها", icon: <ReceiptLongIcon />, color: "#f59e0b" },
  { to: "/payments", label: "پرداخت‌ها", icon: <PaymentsIcon />, color: "#10b981" },
  { to: "/debts", label: "بدهی‌ها", icon: <MoneyOffIcon />, color: "#f43f5e" },
  { to: "/sales", label: "فروش نمایندگان", icon: <BarChartIcon />, color: "#30d158" },
  { to: "/financial-history", label: "تاریخچهٔ مالی", icon: <AccountBalanceIcon />, color: "#14b8a6" },
  { to: "/broadcast", label: "پیام همگانی", icon: <CampaignIcon />, color: "#ec4899" },
  { to: "/logs", label: "گزارش‌ها", icon: <HistoryIcon />, color: "#0891b2" },
  { to: "/account", label: "حساب و پشتیبان", icon: <ManageAccountsIcon />, color: "#3b82f6" },
  { to: "/tools", label: "ابزارها", icon: <BuildIcon />, color: "#8b5cf6" },
  { to: "/settings", label: "تنظیمات", icon: <SettingsIcon />, color: "#64748b" },
  { to: "/help", label: "راهنما", icon: <HelpOutlineIcon />, color: "#06b6d4" },
];

export default function Layout() {
  const theme = useTheme();
  const isDark = theme.palette.mode === "dark";
  const isDesktop = useMediaQuery(theme.breakpoints.up("md"));
  const [open, setOpen] = useState(false);
  const nav = useNavigate();
  const loc = useLocation();
  const { username, logout } = useAuth();
  const { mode, toggle } = useColorMode();
  const primary = theme.palette.primary.main;
  const { data: info } = useQuery({ queryKey: ["app-info"], queryFn: getInfo, staleTime: 600000 });
  useResponsiveTableLabels();

  const navItemSx = (selected: boolean) => ({
    position: "relative",
    borderRadius: 2.5,
    mx: 1.25,
    my: 0.3,
    py: 0.7,
    color: selected ? "text.primary" : "text.secondary",
    backdropFilter: selected ? "blur(16px) saturate(180%)" : "none",
    WebkitBackdropFilter: selected ? "blur(16px) saturate(180%)" : "none",
    "& .MuiListItemIcon-root": { minWidth: 40 },
    "&.Mui-selected": {
      backgroundColor: isDark ? "rgba(255,255,255,.07)" : "rgba(255,255,255,.60)",
      backgroundImage: "linear-gradient(175deg,rgba(255,255,255,.12) 0%,rgba(255,255,255,0) 60%)",
      boxShadow: isDark
        ? "inset 0 1px 0 rgba(255,255,255,.14), 0 2px 12px -6px rgba(0,0,0,.45)"
        : "inset 0 1px 0 rgba(255,255,255,.96), 0 2px 12px -6px rgba(30,40,100,.18)",
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
    <Box
      sx={{
        height: "100%",
        display: "flex",
        flexDirection: "column",
        position: "relative",
        overflow: "hidden",
        // The sidebar glass is provided by MuiDrawer.paper or by the sticky Box below
      }}
    >
      {/* Static ambient accent — no animation to avoid continuous GPU compositing */}
      <Box
        aria-hidden
        sx={{
          position: "absolute",
          inset: 0,
          background: isDark
            ? "radial-gradient(ellipse 120% 80% at 50% 0%, rgba(139,92,246,.28) 0%, transparent 70%)"
            : "radial-gradient(ellipse 120% 80% at 50% 0%, rgba(139,92,246,.20) 0%, transparent 70%)",
          pointerEvents: "none",
          zIndex: 0,
        }}
      />

      <Box sx={{ position: "relative", zIndex: 1, display: "flex", flexDirection: "column", height: "100%" }}>
        <Toolbar sx={{ py: 2.5 }}>
          <Stack direction="row" alignItems="center" spacing={1.25}>
            <Box
              sx={{
                width: 40,
                height: 40,
                borderRadius: 2.5,
                color: "#fff",
                display: "grid",
                placeItems: "center",
                background: "linear-gradient(145deg, #5ab5ff 0%, #0071e3 100%)",
                boxShadow: [
                  "0 6px 18px -6px rgba(0,113,227,.55)",
                  "inset 0 1.5px 0 rgba(255,255,255,.40)",
                ].join(", "),
              }}
            >
              <ReceiptLongIcon fontSize="small" />
            </Box>
            <Box>
              <Typography variant="subtitle1" sx={{ fontWeight: 800, lineHeight: 1.1 }}>
                سامانه فاکتور
              </Typography>
              <Typography variant="caption" color="text.secondary">
                مدیریت نمایندگان
              </Typography>
            </Box>
          </Stack>
        </Toolbar>

        <Divider />

        <List sx={{ py: 1, flexGrow: 1 }}>
          {NAV.map((item) => {
            const selected = loc.pathname === item.to;
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
                      width: 31,
                      height: 31,
                      borderRadius: 2,
                      display: "grid",
                      placeItems: "center",
                      color: item.color,
                      bgcolor: alpha(item.color, isDark ? 0.22 : 0.14),
                      backgroundImage: "linear-gradient(145deg,rgba(255,255,255,.24) 0%,rgba(255,255,255,0) 60%)",
                      boxShadow: selected
                        ? [
                            `0 0 0 1px ${alpha(item.color, 0.50)}`,
                            `0 4px 12px -4px ${alpha(item.color, 0.55)}`,
                            "inset 0 1px 0 rgba(255,255,255,.32)",
                          ].join(", ")
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
            onClick={logout}
            sx={{
              borderRadius: 2.5,
              mx: 1.25,
              my: 0.3,
              py: 0.85,
              color: "error.main",
              "& .MuiListItemIcon-root": { color: "error.main", minWidth: 38 },
              "&:hover": {
                bgcolor: alpha(theme.palette.error.main, 0.08),
                backgroundImage: "none",
              },
            }}
          >
            <ListItemIcon><LogoutIcon /></ListItemIcon>
            <ListItemText
              primary="خروج"
              primaryTypographyProps={{ fontWeight: 600, fontSize: 14.5 }}
            />
          </ListItemButton>
        </List>

        <Box sx={{ py: 1.5, textAlign: "center", borderTop: "1px solid", borderColor: "divider" }}>
          <Typography variant="caption" color="text.secondary" dir="ltr">
            {info?.version ? `v${info.version}` : "…"}
          </Typography>
        </Box>
      </Box>
    </Box>
  );

  // Glass sidebar background for the sticky desktop nav
  const sidebarGlassSx = {
    backdropFilter: "blur(48px) saturate(220%) brightness(1.03)",
    WebkitBackdropFilter: "blur(48px) saturate(220%) brightness(1.03)",
    backgroundColor: isDark ? "rgba(9,11,20,.50)" : "rgba(255,255,255,.55)",
    borderInlineStart: `1px solid ${isDark ? "rgba(255,255,255,.10)" : "rgba(255,255,255,.75)"}`,
    boxShadow: isDark
      ? "inset -1px 0 0 rgba(255,255,255,.05)"
      : "inset -1px 0 0 rgba(255,255,255,.60)",
  };

  return (
    <Box sx={{ display: "flex", minHeight: "100vh" }}>
      {isDesktop ? (
        <Box
          component="nav"
          sx={{
            width: WIDTH,
            flexShrink: 0,
            position: "sticky",
            top: 0,
            alignSelf: "flex-start",
            height: "100vh",
            overflowY: "auto",
            ...sidebarGlassSx,
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
        {/* AppBar — gets glass from MuiAppBar.styleOverrides; only layout attrs here */}
        <AppBar position="sticky" elevation={0} color="transparent"
          sx={{ borderBottom: "1px solid", borderColor: "divider", pt: "env(safe-area-inset-top)" }}>
          <Toolbar>
            {!isDesktop && (
              <IconButton edge="start" onClick={() => setOpen(true)} sx={{ ml: 1 }}>
                <MenuIcon />
              </IconButton>
            )}
            <Typography variant="h6" noWrap sx={{ flexGrow: 1, fontWeight: 800, minWidth: 0 }}>
              {NAV.find((n) => n.to === loc.pathname)?.label || ""}
            </Typography>
            <Tooltip title={mode === "dark" ? "حالت روشن" : "حالت تیره"}>
              <IconButton onClick={toggle} sx={{ mr: 1 }}>
                {mode === "dark" ? <LightModeIcon /> : <DarkModeIcon />}
              </IconButton>
            </Tooltip>
            <Chip
              icon={<PersonOutlineIcon />}
              label={username || "owner"}
              variant="outlined"
              size="small"
              sx={{ display: { xs: "none", sm: "inline-flex" } }}
            />
          </Toolbar>
        </AppBar>

        <Box sx={{ p: { xs: 2, md: 3 }, pb: { xs: "calc(76px + env(safe-area-inset-bottom))", md: 3 }, flexGrow: 1 }}>
          <ErrorBoundary>
            <Suspense
              fallback={
                <Box sx={{ display: "grid", placeItems: "center", py: 12 }}>
                  <CircularProgress />
                </Box>
              }
            >
              <PageTransition key={loc.pathname}>
                <Outlet />
              </PageTransition>
            </Suspense>
          </ErrorBoundary>
        </Box>
      </Box>

      {!isDesktop && <BottomNav onMore={() => setOpen(true)} />}
    </Box>
  );
}
