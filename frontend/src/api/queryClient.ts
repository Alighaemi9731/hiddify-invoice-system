import { QueryClient, keepPreviousData } from "@tanstack/react-query";

// Shared QueryClient instance: main.tsx provides it to the tree, and the axios 401
// interceptor (client.ts) clears it on forced logout so a re-login never serves
// stale invoice/payment data from the pre-logout cache.
//
// staleTime 60s + keepPreviousData: returning to a recently-visited page (or changing a
// filter/period) renders the cached data INSTANTLY instead of a network-gated spinner —
// on high-RTT connections this is the difference between "instant" and "seconds" per
// navigation. Freshness is preserved where it matters: every money mutation invalidates
// its query families (MONEY_KEYS), which refetches regardless of staleTime.
export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      refetchOnWindowFocus: false,
      retry: 1,
      staleTime: 60_000,
      placeholderData: keepPreviousData,
    },
  },
});
