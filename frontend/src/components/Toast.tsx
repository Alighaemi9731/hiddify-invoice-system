import { useState, useCallback } from "react";
import { Snackbar, Alert } from "@mui/material";
import { apiErrorMessage } from "../api/errors";

type Sev = "success" | "error" | "info" | "warning";

export function useToast() {
  const [state, setState] = useState<{ open: boolean; msg: string; sev: Sev }>({
    open: false, msg: "", sev: "success",
  });
  const show = useCallback((msg: string, sev: Sev = "success") => {
    setState({ open: true, msg, sev });
  }, []);
  const node = (
    <Snackbar
      open={state.open}
      autoHideDuration={4500}
      onClose={() => setState((s) => ({ ...s, open: false }))}
      anchorOrigin={{ vertical: "bottom", horizontal: "left" }}
    >
      <Alert severity={state.sev} variant="filled" onClose={() => setState((s) => ({ ...s, open: false }))}>
        {state.msg}
      </Alert>
    </Snackbar>
  );
  return { node, show };
}

// Delegates to the shared map. It used to return `detail` straight through, which for a FastAPI
// validation error is a LIST of dicts — rendered into a React child that throws, so a 422 took the
// whole page down via the ErrorBoundary instead of showing a message.
export const errMsg = (e: unknown) => apiErrorMessage(e, "خطایی رخ داد");
