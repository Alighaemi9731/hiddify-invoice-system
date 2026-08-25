import { ReactNode } from "react";
import { Alert, Box, Button, Card, Skeleton, Stack } from "@mui/material";
import RefreshIcon from "@mui/icons-material/esm/Refresh";
import { apiErrorMessage } from "../api/errors";

/**
 * Wraps a data view (usually a table) with consistent loading + error states. While the
 * query loads, a shimmer skeleton matching the table shape is shown; on failure, a clear
 * Persian error with a retry; otherwise the children render (which handle their own empty
 * state). Drop-in: `<DataState isLoading={isLoading} isError={isError} cols={6} onRetry={refetch}>…`.
 *
 * Pass `error={query.error}` to say what actually went wrong. Without it the message can only be
 * the generic connection sentence, which is wrong for the 404 / 422 / 502 that reach this branch
 * just as often as a real network drop. The prop is optional so call sites adopt it as they are
 * touched rather than in one sweeping edit.
 */
export function DataState({
  isLoading, isError, error, rows = 7, onRetry, children,
}: {
  isLoading?: boolean;
  isError?: boolean;
  error?: unknown;
  rows?: number;
  onRetry?: () => void;
  children: ReactNode;
}) {
  if (isError) {
    return (
      <Alert
        severity="error"
        sx={{ my: 2 }}
        action={onRetry && (
          <Button color="inherit" size="small" startIcon={<RefreshIcon />} onClick={onRetry}>
            تلاش دوباره
          </Button>
        )}
      >
        {error
          ? apiErrorMessage(error, "خطا در بارگذاری اطلاعات. دوباره تلاش کنید.")
          : "خطا در بارگذاری اطلاعات. اتصالِ اینترنت را بررسی کنید و دوباره تلاش کنید."}
      </Alert>
    );
  }
  if (isLoading) {
    return (
      <Card>
        <Box sx={{ p: 1.5 }}>
          <Stack spacing={1}>
            <Skeleton variant="rounded" height={34} sx={{ borderRadius: 1.5, opacity: 0.8 }} />
            {Array.from({ length: rows }).map((_, i) => (
              <Skeleton key={i} variant="rounded" height={40} sx={{ borderRadius: 1.5 }} />
            ))}
          </Stack>
        </Box>
      </Card>
    );
  }
  return <>{children}</>;
}
