import { motion, animate } from "framer-motion";
import { ReactNode, useEffect, useState } from "react";

// A soft, slightly-overshooting ease used across all entrances.
const EASE = [0.22, 1, 0.36, 1] as const;

/**
 * Enter-only page transition. Keyed on the route path by the caller so the SPA
 * remounts (and replays the entrance) on every navigation — no exit jank with
 * react-router's <Outlet>. Fade + a gentle rise (RTL-safe: vertical only).
 */
export function PageTransition({ children }: { children: ReactNode }) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 14 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.4, ease: EASE }}
    >
      {children}
    </motion.div>
  );
}

/**
 * Animate a number from 0 up to `to` on mount (and whenever `to` changes), rendering
 * it through `format` each frame — e.g. a Toman total ticking up. Honours
 * prefers-reduced-motion by snapping to the final value.
 */
export function CountUp({
  to, format, duration = 1.2,
}: { to: number; format: (n: number) => string; duration?: number }) {
  const reduce =
    typeof window !== "undefined" &&
    window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
  const [val, setVal] = useState(reduce ? to : 0);
  useEffect(() => {
    if (reduce) { setVal(to); return; }
    const controls = animate(0, to, {
      duration, ease: [0.22, 1, 0.36, 1], onUpdate: (v) => setVal(v),
    });
    return () => controls.stop();
  }, [to, duration, reduce]);
  return <>{format(val)}</>;
}

/** Wrap a block to fade-rise it in on mount (optionally delayed). */
export function Reveal({ children, delay = 0 }: { children: ReactNode; delay?: number }) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 16 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.45, ease: EASE, delay }}
    >
      {children}
    </motion.div>
  );
}
