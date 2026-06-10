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
  build: {
    chunkSizeWarningLimit: 500,
    rolldownOptions: {
      output: {
        codeSplitting: {
          groups: [
            { name: "vendor-zrender", test: /node_modules[\\/]zrender/, priority: 80 },
            { name: "vendor-chart-react", test: /node_modules[\\/]echarts-for-react/, priority: 70 },
            { name: "vendor-echarts", test: /node_modules[\\/]echarts/, priority: 60, maxSize: 450_000 },
            { name: "vendor-react", test: /node_modules[\\/](react|react-dom|react-router|scheduler)/, priority: 40 },
            { name: "vendor-data", test: /node_modules[\\/](@tanstack|axios)/, priority: 30 },
            { name: "vendor-motion", test: /node_modules[\\/]framer-motion/, priority: 20 },
          ],
        },
      },
    },
  },
  server: { host: true, port: 5173 },
  preview: { host: true, port: 5173 },
});
