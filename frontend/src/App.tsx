import { useEffect, useState, lazy } from "react";
import { Routes, Route, Navigate } from "react-router-dom";
import { Box, CircularProgress } from "@mui/material";
import { useAuth } from "./auth/AuthContext";
import { getSetupStatus } from "./api/client";
import Layout from "./components/Layout";
// Login + Setup stay eager — they're the pre-auth entry points and must paint instantly
// with no chunk flash. Everything behind auth is lazy so each route (and the heavy chart
// libs) splits into its own chunk, keeping the initial bundle small.
import Login from "./pages/Login";
import Setup from "./pages/Setup";
const Dashboard = lazy(() => import("./pages/Dashboard"));
const Panels = lazy(() => import("./pages/Panels"));
const Resellers = lazy(() => import("./pages/Resellers"));
const Invoices = lazy(() => import("./pages/Invoices"));
const Payments = lazy(() => import("./pages/Payments"));
const Debts = lazy(() => import("./pages/Debts"));
const Sales = lazy(() => import("./pages/Sales"));
const FinancialHistory = lazy(() => import("./pages/FinancialHistory"));
const Logs = lazy(() => import("./pages/Logs"));
const Broadcast = lazy(() => import("./pages/Broadcast"));
const AccountBackup = lazy(() => import("./pages/AccountBackup"));
const Help = lazy(() => import("./pages/Help"));
const Settings = lazy(() => import("./pages/Settings"));
import { ReactNode } from "react";

function RequireAuth({ children }: { children: ReactNode }) {
  const { authed, loading } = useAuth();
  if (loading)
    return (
      <Box sx={{ display: "grid", placeItems: "center", height: "100vh" }}>
        <CircularProgress />
      </Box>
    );
  if (!authed) return <Navigate to="/login" replace />;
  return <>{children}</>;
}

function Spinner() {
  return (
    <Box sx={{ display: "grid", placeItems: "center", height: "100vh" }}>
      <CircularProgress />
    </Box>
  );
}

export default function App() {
  // Gate everything on the one-time setup state. Until the owner completes setup,
  // the wizard is shown for ANY path; afterwards it's never shown again.
  const [setupDone, setSetupDone] = useState<boolean | null>(null);

  useEffect(() => {
    let cancelled = false;
    // Retry a few times on transient errors so a brief backend hiccup on a fresh
    // install doesn't skip the setup wizard and strand the user on a login they can't use.
    const attempt = (n: number) => {
      getSetupStatus()
        .then((s) => { if (!cancelled) setSetupDone(s.setup_done); })
        .catch(() => {
          if (cancelled) return;
          if (n > 0) setTimeout(() => attempt(n - 1), 1500);
          else setSetupDone(true); // give up after retries → show login
        });
    };
    attempt(4);
    return () => { cancelled = true; };
  }, []);

  if (setupDone === null) return <Spinner />;
  if (!setupDone) return <Setup onDone={() => setSetupDone(true)} />;

  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route
        element={
          <RequireAuth>
            <Layout />
          </RequireAuth>
        }
      >
        <Route path="/" element={<Dashboard />} />
        <Route path="/panels" element={<Panels />} />
        <Route path="/resellers" element={<Resellers />} />
        <Route path="/invoices" element={<Invoices />} />
        <Route path="/payments" element={<Payments />} />
        <Route path="/debts" element={<Debts />} />
        <Route path="/sales" element={<Sales />} />
        <Route path="/financial-history" element={<FinancialHistory />} />
        <Route path="/broadcast" element={<Broadcast />} />
        <Route path="/logs" element={<Logs />} />
        <Route path="/account" element={<AccountBackup />} />
        <Route path="/help" element={<Help />} />
        <Route path="/settings" element={<Settings />} />
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
