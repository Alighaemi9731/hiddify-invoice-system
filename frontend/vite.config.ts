import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import { VitePWA } from "vite-plugin-pwa";

export default defineConfig({
  plugins: [
    react(),
    // Installable PWA + offline app shell. The service worker precaches the built assets so
    // the UI loads offline; the API is NEVER cached (financial data must stay live), so /api
    // always hits the network. registerType "autoUpdate" + skipWaiting: a new deploy's SW
    // activates IMMEDIATELY on the next navigation (sw.js is served no-cache, so the browser
    // always revalidates it), force-refreshing the bundle. This is deliberate: the "prompt"
    // mode left users stranded on stale precached CSS (the old SW kept serving until every tab
    // closed). The one downside — cleanupOutdatedCaches can drop an old hashed chunk mid-session
    // → a lazy route 404 — is fully mitigated by ErrorBoundary's one-shot chunk-error reload.
    VitePWA({
      registerType: "autoUpdate",
      injectRegister: "auto",
      manifest: false, // keep the existing public/site.webmanifest
      workbox: {
        // Precache ONLY the app shell (entry chunks the first paint needs, the font, the
        // icons). Precaching every lazy route chunk pulled 124 files / ~3.7 MB through a
        // cold connection on FIRST visit (and re-pulled most of it after every release),
        // starving the requests that actually gate first paint. Lazy chunks are instead
        // runtime-cached on first use below — same offline behavior after a route has
        // been visited once, a fraction of the install cost.
        globPatterns: [
          "index.html",
          "assets/index-*.js",
          "assets/rolldown-runtime-*.js",
          "assets/vendor-react-*.js",
          "assets/vendor-emotion-*.js",
          "assets/vendor-data-*.js",
          "assets/Stack-*.js",
          "assets/Grow-*.js",
          "assets/List-*.js",
          "fonts/*.woff2",
          "favicon.svg",
          "favicon-48.png",
          "site.webmanifest",
        ],
        navigateFallback: "/index.html",
        navigateFallbackDenylist: [/^\/api/],
        cleanupOutdatedCaches: true,
        clientsClaim: true,
        skipWaiting: true,
        runtimeCaching: [
          // Hashed build assets: content-addressed, safe to cache-first forever. Keeping
          // superseded hashes around (until expiration) also bridges the mid-deploy
          // window where a lazy chunk of the OLD build is requested after the NEW sw
          // activated — the copy in this cache still serves it (ErrorBoundary's one-shot
          // reload stays as the last-resort backstop).
          {
            urlPattern: /\/assets\/.+\.(js|css|woff2|svg|png)$/,
            handler: "CacheFirst",
            options: {
              cacheName: "hashed-assets",
              expiration: { maxEntries: 300, maxAgeSeconds: 31536000, purgeOnQuotaError: true },
              cacheableResponse: { statuses: [200] },
            },
          },
          // NO API caching — financial data must stay live; /api is denylisted above and
          // deliberately has no runtimeCaching entry.
        ],
      },
    }),
  ],
  build: {
    chunkSizeWarningLimit: 600,
    rolldownOptions: {
      output: {
        codeSplitting: {
          groups: [
            // @emotion/* must outrank vendor-motion: framer-motion also depends on
            // @emotion/is-prop-valid, and without this group that shared module landed in
            // the vendor-motion chunk — making emotion (needed at entry) statically import
            // the whole 40 KB gz framer-motion chunk before Login could paint.
            { name: "vendor-emotion", test: /node_modules[\\/](@emotion|stylis)/, priority: 70 },
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
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    css: true,
  },
});
