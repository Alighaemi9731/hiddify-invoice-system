import { useEffect } from "react";

/**
 * Auto-label table cells for the mobile "card" layout. For every
 * `<table class="resp-table">`, copy each column's header text onto the matching body
 * cell as `data-label`, which the global CSS shows on phones (see theme.ts). This means a
 * page only needs `className="resp-table"` on its <Table> — no per-cell wiring.
 *
 * Mounted once (in Layout). Re-runs on DOM changes (data load / sort / filter), debounced
 * with rAF. Only `childList` is observed, so writing the data-label attribute can't loop.
 */
export function useResponsiveTableLabels(): void {
  useEffect(() => {
    let raf = 0;
    const apply = () => {
      document.querySelectorAll<HTMLTableElement>("table.resp-table").forEach((table) => {
        const heads = Array.from(table.querySelectorAll("thead th")).map(
          (th) => (th.textContent || "").trim()
        );
        if (!heads.length) return;
        table.querySelectorAll("tbody tr").forEach((tr) => {
          const cells = Array.from(tr.children);
          if (cells.length !== heads.length) return; // skip colspan/empty-state rows
          cells.forEach((td, i) => {
            if ((td as HTMLElement).dataset.label !== heads[i]) {
              (td as HTMLElement).dataset.label = heads[i];
            }
          });
        });
      });
    };
    const schedule = () => {
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(apply);
    };
    schedule();
    const obs = new MutationObserver(schedule);
    obs.observe(document.body, { childList: true, subtree: true });
    return () => {
      obs.disconnect();
      cancelAnimationFrame(raf);
    };
  });
}
