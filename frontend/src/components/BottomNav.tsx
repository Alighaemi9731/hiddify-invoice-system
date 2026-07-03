import { Box, BottomNavigation, BottomNavigationAction } from "@mui/material";
import { useLocation, useNavigate } from "react-router-dom";
import DashboardIcon from "@mui/icons-material/esm/Dashboard";
import ReceiptLongIcon from "@mui/icons-material/esm/ReceiptLong";
import PaymentsIcon from "@mui/icons-material/esm/Payments";
import GroupIcon from "@mui/icons-material/esm/Group";
import MoreHorizIcon from "@mui/icons-material/esm/MoreHoriz";

// The four most-used destinations get a one-tap tab bar on phones; every other page is one tap
// away through «بیشتر», which opens the existing full drawer. Hidden on md+ (the sidebar covers it).
const TABS = [
  { value: "/", label: "داشبورد", icon: <DashboardIcon /> },
  { value: "/invoices", label: "فاکتورها", icon: <ReceiptLongIcon /> },
  { value: "/payments", label: "پرداخت‌ها", icon: <PaymentsIcon /> },
  { value: "/resellers", label: "نمایندگان", icon: <GroupIcon /> },
];

export default function BottomNav({ onMore }: { onMore: () => void }) {
  const loc = useLocation();
  const nav = useNavigate();

  // Highlight the current tab; every non-tab route highlights «بیشتر».
  const current =
    loc.pathname === "/"
      ? "/"
      : TABS.find((t) => t.value !== "/" && loc.pathname.startsWith(t.value))?.value ?? "more";

  return (
    <Box
      sx={(t) => ({
        display: { xs: "block", md: "none" },
        position: "fixed",
        insetInline: 0,
        bottom: 0,
        zIndex: t.zIndex.appBar,
        borderTop: "1px solid",
        borderColor: "divider",
        backdropFilter: "blur(40px) saturate(180%)",
        WebkitBackdropFilter: "blur(40px) saturate(180%)",
        backgroundColor: t.palette.mode === "dark" ? "rgba(0,0,0,0.72)" : "rgba(255,255,255,0.86)",
        pb: "env(safe-area-inset-bottom)",
      })}
    >
      <BottomNavigation
        showLabels
        value={current}
        onChange={(_e, v) => (v === "more" ? onMore() : nav(v))}
        sx={{ bgcolor: "transparent", height: 60 }}
      >
        {TABS.map((tab) => (
          <BottomNavigationAction
            key={tab.value}
            value={tab.value}
            label={tab.label}
            icon={tab.icon}
            sx={{ minWidth: 0, "& .MuiBottomNavigationAction-label": { fontSize: 11 } }}
          />
        ))}
        <BottomNavigationAction
          value="more"
          label="بیشتر"
          icon={<MoreHorizIcon />}
          sx={{ minWidth: 0, "& .MuiBottomNavigationAction-label": { fontSize: 11 } }}
        />
      </BottomNavigation>
    </Box>
  );
}
