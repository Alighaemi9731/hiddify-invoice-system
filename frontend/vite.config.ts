import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { VitePWA } from "vite-plugin-pwa";

export default defineConfig({
  plugins: [
    react(),
    // Installable PWA + offline app shell. The service worker precaches the built
    // assets so the UI loads offline; the API is NEVER cached (financial data must stay
    // live), so /api always hits the network. registerType "prompt" (no skipWaiting): a
    // new deploy stays WAITING until the user taps the in-app update toast (UpdateToast.tsx).
    // This avoids the old autoUpdate+skipWaiting hazard where cleanupOutdatedCaches deleted
    // hashed chunks mid-session and an in-flight lazy route could 404.
    VitePWA({
      registerType: "prompt",
      injectRegister: "auto",
      manifest: false, // keep the existing public/site.webmanifest
      workbox: {
        globPatterns: ["**/*.{js,css,html,svg,png,ico,woff2}"],
        navigateFallback: "/index.html",
        navigateFallbackDenylist: [/^\/api/],
        cleanupOutdatedCaches: true,
        clientsClaim: true,
        runtimeCaching: [], // no API/runtime caching — always-fresh data
      },
    }),
  ],
  build: {
    chunkSizeWarningLimit: 600,
    rolldownOptions: {
      output: {
        codeSplitting: {
          groups: [
            { name: "vendor-charts", test: /node_modules[\\/](echarts|zrender)/, priority: 60 },
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
