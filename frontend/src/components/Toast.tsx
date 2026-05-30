import { useState, useCallback } from "react";
import { Snackbar, Alert } from "@mui/material";

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

export const errMsg = (e: any) =>
  e?.response?.data?.detail || e?.message || "خطایی رخ داد";
