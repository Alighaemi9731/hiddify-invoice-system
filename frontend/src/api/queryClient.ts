import { QueryClient } from "@tanstack/react-query";

// Shared QueryClient instance: main.tsx provides it to the tree, and the axios 401
// interceptor (client.ts) clears it on forced logout so a re-login never serves
// stale invoice/payment data from the pre-logout cache.
export const queryClient = new QueryClient({
  defaultOptions: { queries: { refetchOnWindowFocus: false, retry: 1 } },
});
