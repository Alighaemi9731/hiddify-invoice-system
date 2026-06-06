import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { VitePWA } from "vite-plugin-pwa";

export default defineConfig({
  plugins: [
    react(),
    // Installable PWA + offline app shell. The service worker precaches the built
    // assets so the UI loads offline; the API is NEVER cached (financial data must stay
    // live), so /api always hits the network. autoUpdate → a new deploy is picked up and
    // applied on the next navigation (works with nginx's no-cache index.html).
    VitePWA({
      registerType: "autoUpdate",
      injectRegister: "auto",
      manifest: false, // keep the existing public/site.webmanifest
      workbox: {
        globPatterns: ["**/*.{js,css,html,svg,png,ico,woff2}"],
        navigateFallback: "/index.html",
        navigateFallbackDenylist: [/^\/api/],
        cleanupOutdatedCaches: true,
        clientsClaim: true,
        skipWaiting: true,
        runtimeCaching: [], // no API/runtime caching — always-fresh data
      },
    }),
  ],
  server: { host: true, port: 5173 },
  preview: { host: true, port: 5173 },
});
