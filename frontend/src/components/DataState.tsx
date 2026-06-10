import { ReactNode } from "react";
import { Alert, Box, Button, Card, Skeleton, Stack } from "@mui/material";
import RefreshIcon from "@mui/icons-material/esm/Refresh";

/**
 * Wraps a data view (usually a table) with consistent loading + error states. While the
 * query loads, a shimmer skeleton matching the table shape is shown; on failure, a clear
 * Persian error with a retry; otherwise the children render (which handle their own empty
 * state). Drop-in: `<DataState isLoading={isLoading} isError={isError} cols={6} onRetry={refetch}>…`.
 */
export function DataState({
  isLoading, isError, rows = 7, onRetry, children,
}: {
  isLoading?: boolean;
  isError?: boolean;
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
        خطا در بارگذاری اطلاعات. اتصالِ اینترنت را بررسی کنید و دوباره تلاش کنید.
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
