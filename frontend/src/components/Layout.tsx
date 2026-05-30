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
import { useAuth } from "../auth/AuthContext";
import { useColorMode } from "../colorMode";
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

  const navItemSx = (selected: boolean) => ({
    borderRadius: 2.5, mx: 1.25, my: 0.3, py: 0.85,
    color: selected ? "primary.main" : "text.secondary",
    "& .MuiListItemIcon-root": { color: selected ? "primary.main" : "text.secondary", minWidth: 38 },
    "&.Mui-selected": {
      bgcolor: alpha(primary, mode === "dark" ? 0.22 : 0.1),
      "&:hover": { bgcolor: alpha(primary, mode === "dark" ? 0.28 : 0.16) },
    },
    "&:hover": { bgcolor: alpha(primary, mode === "dark" ? 0.14 : 0.06) },
  });

  const sidebar = (
    <Box sx={{ height: "100%", display: "flex", flexDirection: "column", bgcolor: "background.paper" }}>
      <Toolbar sx={{ py: 2.5 }}>
        <Stack direction="row" alignItems="center" spacing={1.25}>
          <Box sx={{ width: 36, height: 36, borderRadius: 2, bgcolor: "primary.main", color: "#fff", display: "grid", placeItems: "center" }}>
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
        <AppBar position="sticky" elevation={0}
          sx={{ bgcolor: "background.paper", color: "text.primary", borderBottom: "1px solid", borderColor: "divider" }}>
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
