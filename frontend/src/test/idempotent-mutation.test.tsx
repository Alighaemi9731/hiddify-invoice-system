/**
 * `useIdempotentMutation`'s in-flight guard.
 *
 * One hook instance serves every command a storefront page can issue. The guard used to be a single
 * slot, so a DIFFERENT command arriving while one was in flight was rejected outright — the action
 * silently did not happen, and the plain `Error` it threw matched none of the error helpers, so the
 * page reported it as a connection problem. Lanes keep the useful half (the same command
 * double-tapped still collapses onto one request) without blocking an unrelated one.
 */
import { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { useIdempotentMutation } from "../portal/storefront/mutation";
import { apiErrorMessage } from "../api/errors";

type Command = { type: string; value?: number };

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

/** A mutation that never settles until the test releases it, so "in flight" is observable. */
function deferred() {
  const calls: Array<{ input: Command; key: string; resolve: () => void }> = [];
  const fn = (input: Command, key: string) => new Promise<string>((resolve) => {
    calls.push({ input, key, resolve: () => resolve("done") });
  });
  return { calls, fn };
}

describe("useIdempotentMutation", () => {
  it("collapses a double-tap of the SAME command onto one request", async () => {
    const { calls, fn } = deferred();
    const { result } = renderHook(
      () => useIdempotentMutation<string, Command>(fn, { commandKey: (v) => v.type }),
      { wrapper },
    );
    act(() => { result.current.mutate({ type: "enabled", value: 1 }); });
    act(() => { result.current.mutate({ type: "enabled", value: 1 }); });
    await waitFor(() => expect(calls).toHaveLength(1));
    act(() => calls[0].resolve());
  });

  it("lets two DIFFERENT commands run at the same time", async () => {
    const { calls, fn } = deferred();
    const { result } = renderHook(
      () => useIdempotentMutation<string, Command>(fn, { commandKey: (v) => v.type }),
      { wrapper },
    );
    act(() => { result.current.mutate({ type: "reorder" }); });
    act(() => { result.current.mutate({ type: "enabled", value: 1 }); });
    await waitFor(() => expect(calls).toHaveLength(2));
    expect(calls[0].key).not.toBe(calls[1].key);
    act(() => { calls.forEach((call) => call.resolve()); });
  });

  it("still refuses a conflicting command in the SAME lane, with a readable reason", async () => {
    const { calls, fn } = deferred();
    const { result } = renderHook(
      () => useIdempotentMutation<string, Command>(fn, { commandKey: (v) => v.type }),
      { wrapper },
    );
    act(() => { result.current.mutate({ type: "update", value: 1 }); });
    await waitFor(() => expect(calls).toHaveLength(1));

    let rejection: unknown = null;
    await act(async () => {
      rejection = await result.current
        .mutateAsync({ type: "update", value: 2 })
        .then(() => null, (error: unknown) => error);
    });
    expect(calls).toHaveLength(1);
    // Not a bare Error any more: it maps to a sentence that says what is actually happening.
    expect(apiErrorMessage(rejection)).toContain("در حال انجام است");
    act(() => calls[0].resolve());
  });

  it("defaults to a single lane when no commandKey is given", async () => {
    const { calls, fn } = deferred();
    const { result } = renderHook(() => useIdempotentMutation<string, Command>(fn), { wrapper });
    act(() => { result.current.mutate({ type: "a" }); });
    await waitFor(() => expect(calls).toHaveLength(1));
    let rejection: unknown = null;
    await act(async () => {
      rejection = await result.current
        .mutateAsync({ type: "b" })
        .then(() => null, (error: unknown) => error);
    });
    expect(calls).toHaveLength(1);
    expect(rejection).toBeInstanceOf(Error);
    act(() => calls[0].resolve());
  });
});
