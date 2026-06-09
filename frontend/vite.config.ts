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
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (!id.includes("node_modules")) return undefined;
          if (id.includes("/zrender/")) return "vendor-zrender";
          if (id.includes("/echarts-for-react/")) return "vendor-chart-react";
          if (id.includes("/echarts/")) return "vendor-echarts";
          if (id.includes("/@mui/") || id.includes("/@emotion/") ||
              id.includes("/stylis")) return "vendor-ui";
          if (id.includes("/react/") || id.includes("/react-dom/") ||
              id.includes("/react-router") || id.includes("/scheduler/"))
            return "vendor-react";
          if (id.includes("/@tanstack/") || id.includes("/axios/")) return "vendor-data";
          if (id.includes("/framer-motion/")) return "vendor-motion";
          return undefined;
        },
      },
    },
  },
  server: { host: true, port: 5173 },
  preview: { host: true, port: 5173 },
});
