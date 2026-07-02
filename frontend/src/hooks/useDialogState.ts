import { Dispatch, SetStateAction, useCallback, useState } from "react";

/**
 * The repeated dialog-with-payload pattern:
 *   const [row, setRow] = useState<T | null>(null)
 *   … open={!!row} onClose={() => setRow(null)} … {row && <content/>}
 *
 * `close()` clears the payload (exactly like the original `setRow(null)`), so
 * guarded dialog content (`{data && …}`) unmounts the same way it did before.
 * `setData` is exposed for dialogs whose payload is edited in place (form fields).
 */
export function useDialogState<T>(): {
  open: boolean;
  data: T | null;
  openWith: (value: T) => void;
  close: () => void;
  setData: Dispatch<SetStateAction<T | null>>;
} {
  const [data, setData] = useState<T | null>(null);
  const openWith = useCallback((value: T) => setData(value), []);
  const close = useCallback(() => setData(null), []);
  return { open: data !== null, data, openWith, close, setData };
}
