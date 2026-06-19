import { lazy, ReactNode } from "react";
import { Routes, Route, Navigate } from "react-router-dom";
import { Box, CircularProgress } from "@mui/material";
import { PortalAuthProvider, usePortalAuth } from "./PortalAuthContext";
import PortalLogin from "./PortalLogin";
import PortalLayout from "./PortalLayout";

const Dashboard = lazy(() => import("./pages/Dashboard"));
const Invoices = lazy(() => import("./pages/Invoices"));
const Payments = lazy(() => import("./pages/Payments"));
const Subs = lazy(() => import("./pages/Subs"));
const Panels = lazy(() => import("./pages/Panels"));
const Support = lazy(() => import("./pages/Support"));
const Help = lazy(() => import("./pages/Help"));

function RequirePortalAuth({ children }: { children: ReactNode }) {
  const { authed, loading } = usePortalAuth();
  if (loading)
    return (
      <Box sx={{ display: "grid", placeItems: "center", height: "100vh" }}>
        <CircularProgress />
      </Box>
    );
  if (!authed) return <Navigate to="/portal/login" replace />;
  return <>{children}</>;
}

// The reseller-facing site. Fully independent of the owner app + its setup gate: own auth
// context, own token (`portal_token`), own layout. Mounted by App for any `/portal/*` path.
export default function PortalApp() {
  return (
    <PortalAuthProvider>
      <Routes>
        <Route path="/portal/login" element={<PortalLogin />} />
        <Route
          element={
            <RequirePortalAuth>
              <PortalLayout />
            </RequirePortalAuth>
          }
        >
          <Route path="/portal" element={<Dashboard />} />
          <Route path="/portal/invoices" element={<Invoices />} />
          <Route path="/portal/payments" element={<Payments />} />
          <Route path="/portal/subs" element={<Subs />} />
          <Route path="/portal/panels" element={<Panels />} />
          <Route path="/portal/support" element={<Support />} />
          <Route path="/portal/help" element={<Help />} />
        </Route>
        <Route path="/portal/*" element={<Navigate to="/portal" replace />} />
      </Routes>
    </PortalAuthProvider>
  );
}
