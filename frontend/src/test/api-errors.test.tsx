/**
 * The error map is the difference between «اتصال را بررسی کنید» for everything and a reseller
 * actually learning what went wrong, so each branch is pinned individually.
 */
import { ThemeProvider } from "@mui/material";
import { render, screen } from "@testing-library/react";
import { AxiosError, AxiosHeaders } from "axios";
import { describe, expect, it } from "vitest";
import { makeTheme } from "../theme";
import { DataState } from "../components/DataState";
import {
  ConcurrentCommandError, apiErrorMessage, describeApiError, isRetriableError,
} from "../api/errors";

/** An AxiosError the way axios itself builds one: `response` present = the server answered. */
function httpError(status: number, data?: unknown, headers?: Record<string, string>) {
  const error = new AxiosError("Request failed", "ERR_BAD_RESPONSE");
  error.response = {
    status,
    statusText: "",
    data,
    headers: new AxiosHeaders(headers || {}),
    config: { headers: new AxiosHeaders() },
  } as never;
  return error;
}

/** No `response` at all — DNS failure, offline, CORS, aborted connection. */
const networkError = () => new AxiosError("Network Error", "ERR_NETWORK");

describe("describeApiError", () => {
  it("classifies every status the portal can actually receive", () => {
    const cases: Array<[unknown, string]> = [
      [networkError(), "network"],
      [httpError(401), "auth"],
      [httpError(403), "auth"],
      [httpError(404, { detail: "Storefront not found" }), "not_found"],
      [httpError(409, { detail: { code: "in_flight" } }), "soft_conflict"],
      [httpError(409, { detail: { code: "unknown" } }), "soft_conflict"],
      [httpError(409, { detail: { code: "config_conflict" } }), "hard_conflict"],
      [httpError(409, { detail: { code: "idempotency_conflict" } }), "hard_conflict"],
      [httpError(422, { detail: { code: "invalid_if_match" } }), "validation"],
      [httpError(426), "insecure"],
      [httpError(429), "rate_limited"],
      [httpError(502, { detail: { code: "external_failure" } }), "external"],
      [httpError(502, { detail: { code: "storefront_bot_unavailable" } }), "external"],
      [httpError(503), "external"],
      [httpError(500, { detail: "post-commit response failed" }), "server"],
      [new ConcurrentCommandError(), "concurrent"],
      [new Error("boom"), "unknown"],
    ];
    for (const [error, kind] of cases) {
      expect(describeApiError(error).kind, JSON.stringify(kind)).toBe(kind);
      expect(apiErrorMessage(error).length).toBeGreaterThan(0);
    }
  });

  it("prefers the server's own Persian message over the generic sentence", () => {
    const error = httpError(422, {
      detail: { code: "below_cost", message: "قیمت از هزینهٔ شما کمتر است." },
    });
    const info = describeApiError(error);
    expect(info.verbatim).toBe(true);
    expect(info.message).toBe("قیمت از هزینهٔ شما کمتر است.");
  });

  it("never renders FastAPI's list-shaped validation detail", () => {
    // Rendering this array into a React child throws and takes the page down via the
    // ErrorBoundary, which is exactly what `errMsg` used to do.
    const error = httpError(422, { detail: [{ loc: ["body", "gb"], msg: "field required" }] });
    const info = describeApiError(error);
    expect(info.verbatim).toBe(false);
    expect(info.message).toBe("مقادیرِ واردشده معتبر نیست؛ فیلدها را بررسی کنید.");
  });

  it("echoes a Persian string detail but never an internal English one", () => {
    // Auth writes Persian FOR the user…
    expect(describeApiError(httpError(400, { detail: "کد امنیتی نادرست است." })).message)
      .toBe("کد امنیتی نادرست است.");
    // …while the tenant 404 hides absent-vs-foreign behind an English string that must not leak.
    expect(describeApiError(httpError(404, { detail: "Storefront not found" })).message)
      .not.toContain("Storefront");
  });

  it("reads Retry-After for a rate limit and falls back to five seconds", () => {
    expect(describeApiError(httpError(429, null, { "retry-after": "12" })).retryAfter).toBe(12);
    expect(describeApiError(httpError(429)).retryAfter).toBe(5);
    expect(apiErrorMessage(httpError(429, null, { "retry-after": "12" }))).toContain("12");
  });

  it("only calls retriable what a retry could actually fix", () => {
    expect(isRetriableError(networkError())).toBe(true);
    expect(isRetriableError(httpError(500))).toBe(true);
    expect(isRetriableError(httpError(502, { detail: { code: "external_failure" } }))).toBe(true);
    // Deterministic outcomes: re-issuing them only doubles the wait before the user is told.
    expect(isRetriableError(httpError(404))).toBe(false);
    expect(isRetriableError(httpError(422, { detail: { code: "validation" } }))).toBe(false);
  });
});

describe("DataState", () => {
  const renderState = (error?: unknown) => render(
    <ThemeProvider theme={makeTheme("light")}>
      <DataState isError error={error}><div>content</div></DataState>
    </ThemeProvider>,
  );

  it("names the real failure when it is given the error", () => {
    renderState(httpError(502, { detail: { code: "storefront_bot_unavailable" } }));
    expect(screen.getByText(/ربات تلگرامِ فروشگاه در دسترس نیست/)).toBeInTheDocument();
  });

  it("keeps the legacy sentence for call sites that have not adopted the prop", () => {
    renderState();
    expect(screen.getByText(/اتصالِ اینترنت را بررسی کنید/)).toBeInTheDocument();
  });
});
