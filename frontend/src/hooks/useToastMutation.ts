import { useMutation, useQueryClient } from "@tanstack/react-query";
import { errMsg } from "../components/Toast";

type Sev = "success" | "error" | "info" | "warning";
type ShowToast = (msg: string, sev?: Sev) => void;

/**
 * Success toast: a fixed message (default "success" severity), a fixed
 * [message, severity] pair, or a function deriving either from the mutation
 * result (and, when needed, the mutate variables).
 */
type SuccessToast<TData, TVariables> =
  | string
  | [string, Sev]
  | ((result: TData, vars: TVariables) => string | [string, Sev]);

/**
 * The repeated page mutation shape: success toast + optional extra side-effect
 * (close a dialog, schedule polls, …) + query invalidation, with the error toast
 * always `errMsg(e)`. `show` comes from the page's own `useToast()` so the toast
 * renders through the page's existing Snackbar node.
 *
 * On success the order mirrors the previous inline handlers exactly:
 * toast → side-effect → invalidation.
 */
export function useToastMutation<TData = any, TVariables = void>(opts: {
  show: ShowToast;
  mutationFn: (vars: TVariables) => Promise<TData>;
  success?: SuccessToast<TData, TVariables>;
  onSuccess?: (result: TData, vars: TVariables) => void;
  /** Root react-query keys to invalidate on success (each as `{ queryKey: [k] }`). */
  invalidate?: string[];
}) {
  const qc = useQueryClient();
  const { show, mutationFn, success, onSuccess, invalidate } = opts;
  return useMutation({
    mutationFn,
    onSuccess: (result: TData, vars: TVariables) => {
      if (success !== undefined) {
        const msg = typeof success === "function" ? success(result, vars) : success;
        if (Array.isArray(msg)) show(msg[0], msg[1]);
        else show(msg);
      }
      onSuccess?.(result, vars);
      (invalidate || []).forEach((k) => qc.invalidateQueries({ queryKey: [k] }));
    },
    onError: (e) => show(errMsg(e), "error"),
  });
}
