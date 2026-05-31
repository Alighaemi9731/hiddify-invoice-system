import { useEffect, useState } from "react";
import { Routes, Route, Navigate } from "react-router-dom";
import { Box, CircularProgress } from "@mui/material";
import { useAuth } from "./auth/AuthContext";
import { getSetupStatus } from "./api/client";
import Layout from "./components/Layout";
import Login from "./pages/Login";
import Setup from "./pages/Setup";
import Dashboard from "./pages/Dashboard";
import Panels from "./pages/Panels";
import Resellers from "./pages/Resellers";
import Invoices from "./pages/Invoices";
import Payments from "./pages/Payments";
import Debts from "./pages/Debts";
import Sales from "./pages/Sales";
import FinancialHistory from "./pages/FinancialHistory";
import Logs from "./pages/Logs";
import Broadcast from "./pages/Broadcast";
import AccountBackup from "./pages/AccountBackup";
import Help from "./pages/Help";
import Settings from "./pages/Settings";
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
    getSetupStatus()
      .then((s) => setSetupDone(s.setup_done))
      .catch(() => setSetupDone(true)); // if status fails, don't block login
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
