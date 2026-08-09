import { lazy, ReactNode } from "react";
import { Routes, Route, Navigate } from "react-router-dom";
import { Box, CircularProgress } from "@mui/material";
import { PortalAuthProvider, usePortalAuth } from "./PortalAuthContext";
import PortalLogin from "./PortalLogin";
import PortalEntry from "./PortalEntry";
import PortalLayout from "./PortalLayout";

const Dashboard = lazy(() => import("./pages/Dashboard"));
const Invoices = lazy(() => import("./pages/Invoices"));
const Payments = lazy(() => import("./pages/Payments"));
const Subs = lazy(() => import("./pages/Subs"));
const Panels = lazy(() => import("./pages/Panels"));
const Support = lazy(() => import("./pages/Support"));
const Help = lazy(() => import("./pages/Help"));
const StorefrontIndexPage = lazy(() => import("./storefront/StorefrontIndexPage"));
const StorefrontShell = lazy(() => import("./storefront/StorefrontShell"));
const StorefrontDashboardPage = lazy(() => import("./storefront/StorefrontDashboardPage"));
const StorefrontHealthPanel = lazy(() => import("./storefront/StorefrontHealthPanel"));
const StorefrontPlansPage = lazy(() => import("./storefront/StorefrontPlansPage"));
const StorefrontSettingsPage = lazy(() => import("./storefront/StorefrontSettingsPage"));
const StorefrontManagersPage = lazy(() => import("./storefront/StorefrontManagersPage"));
const StorefrontPreviewPage = lazy(() => import("./storefront/StorefrontPreviewPage"));
const StorefrontCustomersPage = lazy(() => import("./storefront/CustomersPage"));
const StorefrontCustomerDetailPage = lazy(() => import("./storefront/CustomerDetailPage"));
const StorefrontTopupsPage = lazy(() => import("./storefront/TopupsPage"));
const StorefrontCreditsPage = lazy(() => import("./storefront/StorefrontCreditsPage"));
const StorefrontCampaignsPage = lazy(() => import("./storefront/StorefrontCampaignsPage"));
const StorefrontFinancePage = lazy(() => import("./storefront/StorefrontFinancePage"));

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
        {/* Permanent per-reseller address — never expires (see PortalEntry). */}
        <Route path="/portal/u/:uuid" element={<PortalEntry />} />
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
          <Route path="/portal/storefront" element={<StorefrontIndexPage />} />
          <Route path="/portal/storefront/:shopId" element={<StorefrontShell />}>
            <Route index element={<StorefrontDashboardPage />} />
            <Route path="finance" element={<StorefrontFinancePage />} />
            <Route path="plans" element={<StorefrontPlansPage />} />
            <Route path="customers" element={<StorefrontCustomersPage />} />
            <Route path="customers/:customerId" element={<StorefrontCustomerDetailPage />} />
            <Route path="topups" element={<StorefrontTopupsPage />} />
            <Route path="credits" element={<StorefrontCreditsPage />} />
            <Route path="campaigns" element={<StorefrontCampaignsPage />} />
            <Route path="settings" element={<StorefrontSettingsPage />} />
            <Route path="managers" element={<StorefrontManagersPage />} />
            <Route path="preview" element={<StorefrontPreviewPage />} />
            <Route path="health" element={<StorefrontHealthPanel />} />
          </Route>
        </Route>
        <Route path="/portal/*" element={<Navigate to="/portal" replace />} />
      </Routes>
    </PortalAuthProvider>
  );
}
