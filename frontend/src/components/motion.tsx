import { motion, Variants } from "framer-motion";
import { Box, BoxProps } from "@mui/material";
import { ReactNode } from "react";

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

// Stagger container + item — reveal grids/lists one-by-one.
export const staggerContainer: Variants = {
  hidden: {},
  show: { transition: { staggerChildren: 0.07, delayChildren: 0.05 } },
};

export const staggerItem: Variants = {
  hidden: { opacity: 0, y: 18 },
  show: { opacity: 1, y: 0, transition: { duration: 0.45, ease: EASE } },
};

/** A motion-enabled MUI Box (so sx + layout animations compose). */
export const MotionBox = motion(Box) as React.FC<BoxProps & React.ComponentProps<typeof motion.div>>;

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
