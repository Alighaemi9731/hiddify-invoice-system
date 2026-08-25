import axios from "axios";

/**
 * One honest translation from an API failure to a Persian sentence.
 *
 * Before this, every storefront page ended its error branch with «…اتصال را بررسی کنید», so a dead
 * shop-bot token, a rejected field, an expired session and a genuine network drop were all reported
 * to the reseller as "check your internet" — and the backend's OWN Persian explanation, which it
 * already ships in `detail.message` for every `AdminCommandError`, was thrown away unread.
 *
 * Priority is deliberate: the server's own words win whenever it wrote any, because it knows things
 * the client cannot (which field, which floor, which shop). The status table below is the fallback
 * for the errors that carry no message — including FastAPI's own validation errors, whose `detail`
 * is a LIST and must never be rendered into the UI.
 */

export type ApiErrorKind =
  | "network"        // the request never got an answer
  | "auth"           // 401/403 — session gone or not permitted
  | "not_found"      // 404
  | "soft_conflict"  // 409 in_flight / unknown — the outcome is genuinely undecided
  | "hard_conflict"  // 409 config_conflict / idempotency_conflict — reload and reapply
  | "validation"     // 4xx over the request itself
  | "insecure"       // 426 — HTTPS required
  | "rate_limited"   // 429
  | "external"       // 502/503/504 — Telegram or the panel, not us and not the user
  | "server"         // other 5xx
  | "concurrent"     // a different command on this page is still in flight
  | "unknown";

export interface ApiErrorInfo {
  kind: ApiErrorKind;
  status: number | null;
  code: string | null;
  message: string;
  retryAfter: number | null;
  /** True when `message` is the SERVER's own text rather than one of the sentences below. */
  verbatim: boolean;
}

/** Thrown by `useIdempotentMutation` when a different command is already in flight. */
export class ConcurrentCommandError extends Error {
  constructor() {
    super("another storefront command is already in progress");
    this.name = "ConcurrentCommandError";
  }
}

const TEXT: Record<Exclude<ApiErrorKind, "rate_limited">, string> = {
  network: "ارتباط با سرور برقرار نشد؛ اتصالِ اینترنت را بررسی کنید و دوباره تلاش کنید.",
  auth: "دسترسی ندارید یا نشستِ شما منقضی شده است؛ دوباره وارد شوید.",
  not_found: "این مورد پیدا نشد یا دیگر در دسترس نیست؛ فهرست را تازه کنید.",
  soft_conflict: "نتیجهٔ عملیات قبلی هنوز مشخص نیست؛ پیش از تلاش دوباره وضعیت فعلی را بررسی کنید.",
  hard_conflict: "این اطلاعات هم‌زمان جای دیگری تغییر کرده است؛ نسخهٔ تازه را بارگذاری کنید و دوباره تلاش کنید.",
  validation: "مقادیرِ واردشده معتبر نیست؛ فیلدها را بررسی کنید.",
  insecure: "برای این درخواست اتصالِ امن (HTTPS) لازم است.",
  external: "ربات تلگرامِ فروشگاه در دسترس نیست (توکن نامعتبر است یا تلگرام پاسخ نمی‌دهد)؛ وضعیتِ ربات را در بخش «سلامت» بررسی کنید.",
  server: "خطای سرور؛ چند لحظه بعد دوباره تلاش کنید.",
  concurrent: "یک عملیاتِ دیگر هنوز در حال انجام است؛ تا پایانِ آن صبر کنید.",
  unknown: "انجام عملیات ناموفق بود؛ دوباره تلاش کنید.",
};

const SOFT_CONFLICT_CODES = ["in_flight", "unknown"];
const HARD_CONFLICT_CODES = ["config_conflict", "idempotency_conflict"];
const EXTERNAL_CODES = ["external_failure", "external_unknown", "storefront_bot_unavailable"];

// Persian wording for the soft-409s, which are two genuinely different situations: one is "wait",
// the other is "we do not know whether it happened". Kept distinct because the second one asks the
// reseller to go and LOOK before retrying a money command.
const SOFT_TEXT: Record<string, string> = {
  in_flight: "عملیات قبلی هنوز در حال اجراست؛ کمی صبر کنید و سپس وضعیت فعلی را بررسی کنید.",
  unknown: "نتیجهٔ عملیات قبلی نامشخص است؛ پیش از تلاش دوباره، وضعیت فعلی را بررسی و با پشتیبانی هماهنگ کنید.",
};

/**
 * A bare-string `detail` is two different things in this API, and only one of them may be shown.
 *
 * Endpoints that speak to the user write Persian ("کد امنیتی نادرست است."). Internal guards write
 * English ("Storefront not found" — the tenant 404, which deliberately does not distinguish absent
 * from foreign and must never be echoed). Testing for Persian letters is exactly the question worth
 * asking: was this sentence written for this reader? Structured errors are unaffected — they carry
 * `{code, message}` and always come from `_raise_admin_error`.
 */
const PERSIAN = /[؀-ۿ]/;

function detailOf(error: unknown): { code: string | null; message: string | null } {
  if (!axios.isAxiosError(error)) return { code: null, message: null };
  const detail = (error.response?.data as { detail?: unknown } | undefined)?.detail;
  // FastAPI's request-validation errors put a LIST here. It is machine-readable, never a sentence,
  // and rendering it into a React child throws — so it deliberately yields no message.
  if (typeof detail === "string") {
    const text = detail.trim();
    return { code: null, message: text && PERSIAN.test(text) ? text : null };
  }
  if (detail && typeof detail === "object" && !Array.isArray(detail)) {
    const record = detail as { code?: unknown; message?: unknown };
    const message = typeof record.message === "string" ? record.message.trim() : "";
    return {
      code: typeof record.code === "string" ? record.code : null,
      message: message || null,
    };
  }
  return { code: null, message: null };
}

function kindOf(status: number | null, code: string | null): ApiErrorKind {
  if (code && EXTERNAL_CODES.includes(code)) return "external";
  if (status === null) return "network";
  if (status === 401 || status === 403) return "auth";
  if (status === 404) return "not_found";
  if (status === 409) {
    if (code && SOFT_CONFLICT_CODES.includes(code)) return "soft_conflict";
    if (code && HARD_CONFLICT_CODES.includes(code)) return "hard_conflict";
    return "hard_conflict";
  }
  if (status === 426) return "insecure";
  if (status === 429) return "rate_limited";
  if (status >= 502 && status <= 504) return "external";
  if (status >= 500) return "server";
  if (status >= 400) return "validation";
  return "unknown";
}

function retryAfterOf(error: unknown): number | null {
  if (!axios.isAxiosError(error) || error.response?.status !== 429) return null;
  const header = error.response.headers?.["retry-after"];
  const seconds = Number(Array.isArray(header) ? header[0] : header);
  return Number.isFinite(seconds) && seconds > 0 ? Math.ceil(seconds) : 5;
}

export function describeApiError(error: unknown, fallback?: string): ApiErrorInfo {
  if (error instanceof ConcurrentCommandError) {
    return {
      kind: "concurrent", status: null, code: null,
      message: TEXT.concurrent, retryAfter: null, verbatim: false,
    };
  }
  if (!axios.isAxiosError(error)) {
    return {
      kind: "unknown", status: null, code: null,
      message: fallback || TEXT.unknown, retryAfter: null, verbatim: false,
    };
  }
  const status = error.response ? error.response.status : null;
  const { code, message } = detailOf(error);
  const kind = kindOf(status, code);
  const retryAfter = retryAfterOf(error);
  if (kind === "soft_conflict" && code && SOFT_TEXT[code]) {
    return { kind, status, code, message: SOFT_TEXT[code], retryAfter, verbatim: false };
  }
  // The server's own words win — but only for the errors it wrote FOR the user. A 5xx `detail` is
  // an internal note ("post-commit response failed"), often English, and showing it to a reseller
  // is both unhelpful and a small information leak.
  if (message && kind !== "server") {
    return { kind, status, code, message, retryAfter, verbatim: true };
  }
  if (kind === "rate_limited") {
    return {
      kind, status, code, retryAfter, verbatim: false,
      message: `تعدادِ درخواست‌ها زیاد بود؛ ${retryAfter ?? 5} ثانیهٔ دیگر دوباره تلاش کنید.`,
    };
  }
  return {
    kind, status, code, retryAfter, verbatim: false,
    message: kind === "unknown" ? (fallback || TEXT.unknown) : TEXT[kind],
  };
}

export const apiErrorMessage = (error: unknown, fallback?: string) =>
  describeApiError(error, fallback).message;

/**
 * Whether re-issuing the request could plausibly succeed. A 404 or a rejected field is decided —
 * retrying it just doubles every deterministic failure before the user is told about it.
 */
export function isRetriableError(error: unknown): boolean {
  const { kind } = describeApiError(error);
  return kind === "network" || kind === "server" || kind === "external";
}
