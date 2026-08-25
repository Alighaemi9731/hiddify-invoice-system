import { useRef } from "react";
import { useMutation, type UseMutationOptions } from "@tanstack/react-query";
import axios from "axios";
import { ConcurrentCommandError, apiErrorMessage } from "../../api/errors";

const newIdempotencyKey = () => {
  if (typeof globalThis.crypto?.randomUUID === "function") return globalThis.crypto.randomUUID();
  return `${Date.now()}-${Math.random().toString(36).slice(2)}`;
};

interface CommandSlot<TData> {
  inFlight: Promise<TData> | null;
  key: string | null;
  fingerprint: string | null;
}

/**
 * `commandKey` groups variables into INDEPENDENT command lanes.
 *
 * One hook instance multiplexes every command a page can issue (create / update / enable / delete /
 * reorder …). With a single shared slot, moving a plan up while a toggle was still in flight was
 * rejected outright — the action silently did not happen, and the plain `Error` it threw matched
 * none of the error helpers, so the page blamed the network. Lanes keep the useful half of the
 * guard (the SAME command double-tapped still de-duplicates onto one request) without blocking a
 * different one.
 *
 * Deliberately not a queue: a queued second command would carry the `If-Match` ETag that the first
 * one is about to bump, so it would replay into a 409 while looking like it had succeeded.
 */
export function useIdempotentMutation<TData, TVariables>(
  mutationFn: (variables: TVariables, idempotencyKey: string) => Promise<TData>,
  options?: Omit<UseMutationOptions<TData, Error, TVariables>, "mutationFn"> & {
    commandKey?: (variables: TVariables) => string;
  },
) {
  const slots = useRef<Map<string, CommandSlot<TData>>>(new Map());
  const { commandKey, ...mutationOptions } = options || {};
  return useMutation({
    ...mutationOptions,
    mutationFn: (variables) => {
      const lane = commandKey ? commandKey(variables) : "default";
      let slot = slots.current.get(lane);
      if (!slot) {
        slot = { inFlight: null, key: null, fingerprint: null };
        slots.current.set(lane, slot);
      }
      const nextFingerprint = JSON.stringify(variables);
      if (slot.inFlight) {
        if (slot.fingerprint === nextFingerprint) return slot.inFlight;
        return Promise.reject(new ConcurrentCommandError());
      }
      if (!slot.key || slot.fingerprint !== nextFingerprint) {
        slot.key = newIdempotencyKey();
        slot.fingerprint = nextFingerprint;
      }
      const current = slot;
      const requestKey = current.key;
      const request = mutationFn(variables, requestKey).then((result) => {
        current.key = null;
        current.fingerprint = null;
        return result;
      }).catch((error: unknown) => {
        // A response means the server reached a definite HTTP outcome (including cached 4xx/5xx),
        // so a deliberate retry must be a new command. Only transport failures with no response
        // retain the key because the server may have committed before the connection failed.
        if (axios.isAxiosError(error) && error.response && isDefinitiveResponse(error.response)) {
          current.key = null;
          current.fingerprint = null;
        }
        throw error;
      }).finally(() => {
        current.inFlight = null;
      });
      current.inFlight = request;
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

// A hard conflict that warrants the reload/reapply dialog: either a shop-config version clash
// (config_conflict) or an idempotency-key payload clash (idempotency_conflict). A soft 409
// (in_flight / unknown) is NOT one of these — it is surfaced via commandRecoveryMessage instead.
export const isConflict = (error: unknown) =>
  axios.isAxiosError(error)
  && error.response?.status === 409
  && ["config_conflict", "idempotency_conflict"].includes(
    (error.response?.data as { detail?: { code?: string } } | undefined)?.detail?.code ?? "",
  );

// A rate-limited live refresh (HTTP 429) is a soft outcome, not a failure. Returns the
// server's Retry-After in whole seconds (defaulting to 5s when the header is absent/invalid),
// or null when the error is not a 429 — so the caller can say "try again in N seconds".
export function rateLimitRetryAfter(error: unknown): number | null {
  if (!axios.isAxiosError(error) || error.response?.status !== 429) return null;
  const header = error.response.headers?.["retry-after"];
  const seconds = Number(Array.isArray(header) ? header[0] : header);
  return Number.isFinite(seconds) && seconds > 0 ? Math.ceil(seconds) : 5;
}

export function commandRecoveryMessage(error: unknown) {
  if (!axios.isAxiosError(error)) return null;
  const code = (error.response?.data as { detail?: { code?: string } } | undefined)?.detail?.code;
  if (code === "in_flight") return "عملیات قبلی هنوز در حال اجراست؛ کمی صبر کنید و سپس وضعیت فعلی را بررسی کنید.";
  if (code === "unknown") return "نتیجهٔ عملیات قبلی نامشخص است؛ پیش از تلاش دوباره، وضعیت فعلی را بررسی و با پشتیبانی هماهنگ کنید.";
  return null;
}

/**
 * What to show a reseller when a storefront command fails. `belowCostMessage` stays first because
 * the server authors that one word for word (including the «۵۰ هزار تومان → 50000» hint);
 * everything else falls through to the shared map, which finally reads the Persian `message` the
 * backend already ships instead of blaming the user's internet connection.
 */
export const storefrontErrorMessage = (error: unknown, fallback?: string) =>
  belowCostMessage(error) ?? apiErrorMessage(error, fallback);

// A plan priced under the reseller's own cost. The server authors the (Persian) explanation —
// including the "you probably meant 50000, not 50" hint — so render it verbatim rather than
// reconstructing it here. Kept separate from commandRecoveryMessage so that helper's soft-409
// recovery semantics stay intact.
export function belowCostMessage(error: unknown) {
  if (!axios.isAxiosError(error) || error.response?.status !== 422) return null;
  const detail = (error.response.data as { detail?: { code?: string; message?: string } } | undefined)?.detail;
  return detail?.code === "below_cost" ? (detail.message ?? null) : null;
}

function isDefinitiveResponse(response: { status: number; data?: unknown }) {
  const code = (response.data as { detail?: { code?: string } } | undefined)?.detail?.code;
  if (response.status === 409 && (code === "in_flight" || code === "unknown")) return false;
  if (response.status < 500) return true;
  return code === "external_failure";
}
