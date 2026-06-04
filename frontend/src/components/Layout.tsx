import { useState } from "react";
import { Outlet, useLocation, useNavigate } from "react-router-dom";
import {
  AppBar, Box, Chip, Drawer, IconButton, List, ListItemButton, ListItemIcon,
  ListItemText, Stack, Toolbar, Typography, Divider, Tooltip, useMediaQuery,
} from "@mui/material";
import { alpha, useTheme } from "@mui/material/styles";
import DashboardIcon from "@mui/icons-material/Dashboard";
import DnsIcon from "@mui/icons-material/Dns";
import GroupIcon from "@mui/icons-material/Group";
import ReceiptLongIcon from "@mui/icons-material/ReceiptLong";
import PaymentsIcon from "@mui/icons-material/Payments";
import MoneyOffIcon from "@mui/icons-material/MoneyOff";
import BarChartIcon from "@mui/icons-material/BarChart";
import AccountBalanceIcon from "@mui/icons-material/AccountBalance";
import HistoryIcon from "@mui/icons-material/History";
import CampaignIcon from "@mui/icons-material/Campaign";
import ManageAccountsIcon from "@mui/icons-material/ManageAccounts";
import HelpOutlineIcon from "@mui/icons-material/HelpOutline";
import SettingsIcon from "@mui/icons-material/Settings";
import LogoutIcon from "@mui/icons-material/Logout";
import MenuIcon from "@mui/icons-material/Menu";
import PersonOutlineIcon from "@mui/icons-material/PersonOutline";
import DarkModeIcon from "@mui/icons-material/DarkModeOutlined";
import LightModeIcon from "@mui/icons-material/LightModeOutlined";
import { useQuery } from "@tanstack/react-query";
import { useAuth } from "../auth/AuthContext";
import { useColorMode } from "../colorMode";
import { getInfo } from "../api/client";
import ErrorBoundary from "./ErrorBoundary";

const WIDTH = 256;

const NAV = [
  { to: "/", label: "داشبورد", icon: <DashboardIcon /> },
  { to: "/panels", label: "پنل‌ها", icon: <DnsIcon /> },
  { to: "/resellers", label: "نمایندگان", icon: <GroupIcon /> },
  { to: "/invoices", label: "فاکتورها", icon: <ReceiptLongIcon /> },
  { to: "/payments", label: "پرداخت‌ها", icon: <PaymentsIcon /> },
  { to: "/debts", label: "بدهی‌ها", icon: <MoneyOffIcon /> },
  { to: "/sales", label: "فروش نمایندگان", icon: <BarChartIcon /> },
  { to: "/financial-history", label: "تاریخچهٔ مالی", icon: <AccountBalanceIcon /> },
  { to: "/broadcast", label: "پیام همگانی", icon: <CampaignIcon /> },
  { to: "/logs", label: "گزارش‌ها", icon: <HistoryIcon /> },
  { to: "/account", label: "حساب و پشتیبان", icon: <ManageAccountsIcon /> },
  { to: "/settings", label: "تنظیمات", icon: <SettingsIcon /> },
  { to: "/help", label: "راهنما", icon: <HelpOutlineIcon /> },
];

export default function Layout() {
  const theme = useTheme();
  const isDesktop = useMediaQuery(theme.breakpoints.up("md"));
  const [open, setOpen] = useState(false);
  const nav = useNavigate();
  const loc = useLocation();
  const { username, logout } = useAuth();
  const { mode, toggle } = useColorMode();
  const primary = theme.palette.primary.main;
  const { data: info } = useQuery({ queryKey: ["app-info"], queryFn: getInfo, staleTime: 600000 });

  const navItemSx = (selected: boolean) => ({
    position: "relative", borderRadius: 2.5, mx: 1.25, my: 0.3, py: 0.85,
    color: selected ? "primary.main" : "text.secondary",
    "& .MuiListItemIcon-root": { color: selected ? "primary.main" : "text.secondary", minWidth: 38 },
    "&.Mui-selected": {
      bgcolor: alpha(primary, mode === "dark" ? 0.18 : 0.09),
      "&:hover": { bgcolor: alpha(primary, mode === "dark" ? 0.24 : 0.14) },
      // accent bar on the leading edge (right in RTL)
      "&::before": {
        content: '""', position: "absolute", insetInlineStart: 4, top: 9, bottom: 9,
        width: 3, borderRadius: 3, bgcolor: "primary.main",
      },
    },
    "&:hover": { bgcolor: alpha(primary, mode === "dark" ? 0.12 : 0.05) },
  });

  const sidebar = (
    <Box sx={{ height: "100%", display: "flex", flexDirection: "column", bgcolor: "background.paper" }}>
      <Toolbar sx={{ py: 2.5 }}>
        <Stack direction="row" alignItems="center" spacing={1.25}>
          <Box sx={{
            width: 38, height: 38, borderRadius: 2.5, color: "#fff", display: "grid", placeItems: "center",
            background: "linear-gradient(135deg, #a78bfa 0%, #6d5efc 55%, #5b50e6 100%)",
            boxShadow: "0 6px 16px -6px rgba(109,94,252,.6)",
          }}>
            <ReceiptLongIcon fontSize="small" />
          </Box>
          <Box>
            <Typography variant="subtitle1" sx={{ fontWeight: 800, lineHeight: 1.1 }}>سامانه فاکتور</Typography>
            <Typography variant="caption" color="text.secondary">مدیریت نمایندگان</Typography>
          </Box>
        </Stack>
      </Toolbar>
      <Divider />
      <List sx={{ py: 1 }}>
        {NAV.map((item) => {
          const selected = loc.pathname === item.to;
          return (
            <ListItemButton key={item.to} selected={selected}
              onClick={() => { nav(item.to); setOpen(false); }} sx={navItemSx(selected)}>
              <ListItemIcon>{item.icon}</ListItemIcon>
              <ListItemText primary={item.label}
                primaryTypographyProps={{ fontWeight: selected ? 700 : 500, fontSize: 14.5 }} />
            </ListItemButton>
          );
        })}
        <Divider sx={{ mx: 1.5, my: 1 }} />
        <ListItemButton
          onClick={logout}
          sx={{
            borderRadius: 2.5, mx: 1.25, my: 0.3, py: 0.85, color: "error.main",
            "& .MuiListItemIcon-root": { color: "error.main", minWidth: 38 },
            "&:hover": { bgcolor: alpha(theme.palette.error.main, 0.08) },
          }}
        >
          <ListItemIcon><LogoutIcon /></ListItemIcon>
          <ListItemText primary="خروج" primaryTypographyProps={{ fontWeight: 600, fontSize: 14.5 }} />
        </ListItemButton>
      </List>
      <Box sx={{ mt: "auto", py: 1.5, textAlign: "center" }}>
        <Typography variant="caption" color="text.secondary" dir="ltr">
          {info?.version ? `v${info.version}` : "…"}
        </Typography>
      </Box>
    </Box>
  );

  return (
    <Box sx={{ display: "flex", minHeight: "100vh" }}>
      {isDesktop ? (
        <Box component="nav" sx={{
          width: WIDTH, flexShrink: 0, borderInlineStart: "1px solid", borderColor: "divider",
          position: "sticky", top: 0, alignSelf: "flex-start", height: "100vh", overflowY: "auto",
        }}>
          {sidebar}
        </Box>
      ) : (
        <Drawer variant="temporary" anchor="right" open={open} onClose={() => setOpen(false)}
          ModalProps={{ keepMounted: true }}
          sx={{ "& .MuiDrawer-paper": { width: WIDTH, boxSizing: "border-box" } }}>
          {sidebar}
        </Drawer>
      )}

      <Box component="main" sx={{ flexGrow: 1, minWidth: 0, display: "flex", flexDirection: "column" }}>
        <AppBar position="sticky" elevation={0} color="transparent"
          sx={{
            bgcolor: (t) => t.palette.mode === "dark" ? "rgba(13,15,26,.72)" : "rgba(255,255,255,.72)",
            backdropFilter: "saturate(180%) blur(10px)", color: "text.primary",
            borderBottom: "1px solid", borderColor: "divider",
          }}>
          <Toolbar>
            {!isDesktop && (
              <IconButton edge="start" onClick={() => setOpen(true)} sx={{ ml: 1 }}><MenuIcon /></IconButton>
            )}
            <Typography variant="h6" sx={{ flexGrow: 1 }}>
              {NAV.find((n) => n.to === loc.pathname)?.label || ""}
            </Typography>
            <Tooltip title={mode === "dark" ? "حالت روشن" : "حالت تیره"}>
              <IconButton onClick={toggle} sx={{ mr: 1 }}>
                {mode === "dark" ? <LightModeIcon /> : <DarkModeIcon />}
              </IconButton>
            </Tooltip>
            <Chip icon={<PersonOutlineIcon />} label={username || "owner"} variant="outlined" size="small" />
          </Toolbar>
        </AppBar>
        <Box sx={{ p: { xs: 2, md: 3 }, flexGrow: 1 }}>
          <ErrorBoundary>
            <Outlet />
          </ErrorBoundary>
        </Box>
      </Box>
    </Box>
  );
}
