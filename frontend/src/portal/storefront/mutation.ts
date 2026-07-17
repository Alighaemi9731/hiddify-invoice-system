import { useRef } from "react";
import { useMutation, type UseMutationOptions } from "@tanstack/react-query";
import axios from "axios";

const newIdempotencyKey = () => {
  if (typeof globalThis.crypto?.randomUUID === "function") return globalThis.crypto.randomUUID();
  return `${Date.now()}-${Math.random().toString(36).slice(2)}`;
};

export function useIdempotentMutation<TData, TVariables>(
  mutationFn: (variables: TVariables, idempotencyKey: string) => Promise<TData>,
  options?: Omit<UseMutationOptions<TData, Error, TVariables>, "mutationFn">,
) {
  const inFlight = useRef<Promise<TData> | null>(null);
  const key = useRef<string | null>(null);
  const fingerprint = useRef<string | null>(null);
  return useMutation({
    ...options,
    mutationFn: (variables) => {
      const nextFingerprint = JSON.stringify(variables);
      if (inFlight.current) {
        if (fingerprint.current === nextFingerprint) return inFlight.current;
        return Promise.reject(new Error("another storefront command is already in progress"));
      }
      if (!key.current || fingerprint.current !== nextFingerprint) {
        key.current = newIdempotencyKey();
        fingerprint.current = nextFingerprint;
      }
      const requestKey = key.current;
      const request = mutationFn(variables, requestKey).then((result) => {
        key.current = null;
        fingerprint.current = null;
        return result;
      }).catch((error: unknown) => {
        // A response means the server reached a definite HTTP outcome (including cached 4xx/5xx),
        // so a deliberate retry must be a new command. Only transport failures with no response
        // retain the key because the server may have committed before the connection failed.
        if (axios.isAxiosError(error) && isDefinitiveResponse(error.response)) {
          key.current = null;
          fingerprint.current = null;
        }
        throw error;
      }).finally(() => {
        inFlight.current = null;
      });
      inFlight.current = request;
      return request;
    },
  });
}

export const isVersionConflict = (error: unknown) =>
  axios.isAxiosError(error)
  && error.response?.status === 409
  && (error.response?.data as { detail?: { code?: string } } | undefined)?.detail?.code === "config_conflict";

export const isNotFound = (error: unknown) =>
  axios.isAxiosError(error) && error.response?.status === 404;

export function commandRecoveryMessage(error: unknown) {
  if (!axios.isAxiosError(error)) return null;
  const code = (error.response?.data as { detail?: { code?: string } } | undefined)?.detail?.code;
  if (code === "in_flight") return "عملیات قبلی هنوز در حال اجراست؛ کمی صبر کنید و سپس وضعیت فعلی را بررسی کنید.";
  if (code === "unknown") return "نتیجهٔ عملیات قبلی نامشخص است؛ پیش از تلاش دوباره، وضعیت فعلی را بررسی و با پشتیبانی هماهنگ کنید.";
  return null;
}

function isDefinitiveResponse(response: { status: number; data?: unknown }) {
  const code = (response.data as { detail?: { code?: string } } | undefined)?.detail?.code;
  if (response.status === 409 && (code === "in_flight" || code === "unknown")) return false;
  if (response.status < 500) return true;
  return code === "external_failure";
}
