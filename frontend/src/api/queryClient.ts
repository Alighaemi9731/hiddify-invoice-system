import { QueryClient, keepPreviousData } from "@tanstack/react-query";
import { isRetriableError } from "./errors";

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
      // Retry only what a retry could fix. A 404 or a rejected field is already decided, so
      // re-issuing it just doubled every deterministic failure — and doubled the wait before the
      // user was told anything.
      retry: (failureCount: number, error: unknown) =>
        failureCount < 1 && isRetriableError(error),
      staleTime: 60_000,
      placeholderData: keepPreviousData,
    },
  },
});
