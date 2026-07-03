import { ReactNode } from "react";
import { Box, Tab, Tabs } from "@mui/material";
import { alpha, useTheme } from "@mui/material/styles";

export interface TabDef {
  label: string;
  icon?: ReactNode;
}

interface Props {
  value: number;
  onChange: (value: number) => void;
  tabs: TabDef[];
  fullWidth?: boolean;
}

/**
 * Pill-style segmented tab control — consistent across all pages.
 * Matches the Resellers page tab switcher design.
 */
export default function SegmentedTabs({ value, onChange, tabs, fullWidth }: Props) {
  const theme = useTheme();
  const isDark = theme.palette.mode === "dark";
  const primary = theme.palette.primary.main;

  return (
    <Box
      sx={{
        p: 0.45,
        border: 1,
        borderColor: "divider",
        borderRadius: "50px",
        bgcolor: isDark ? "rgba(255,255,255,0.05)" : "rgba(255,255,255,0.55)",
        backdropFilter: "blur(12px) saturate(180%)",
        WebkitBackdropFilter: "blur(12px) saturate(180%)",
        width: fullWidth ? "100%" : "fit-content",
        maxWidth: "100%",   // never exceed the viewport → the Tabs scroller handles overflow
        overflow: "hidden", // clip to the rounded pill; horizontal scroll lives on the scroller
      }}
    >
      <Tabs
        value={value}
        onChange={(_, v) => onChange(v)}
        variant={fullWidth ? "fullWidth" : "scrollable"}
        scrollButtons="auto"
        allowScrollButtonsMobile
        sx={{
          minHeight: 38,
          "& .MuiTabs-indicator": { display: "none" },
          // NOTE: do NOT force the scroller to overflow:visible — that defeats the scrollable
          // variant and clips the last tab on narrow screens (the absent-resellers segment was
          // unreachable). Let MUI manage the scroller's overflow so it scrolls horizontally.
          "& .MuiTab-root": {
            minHeight: 38,
            px: 2,
            py: 0.7,
            borderRadius: "50px",
            color: "text.secondary",
            fontWeight: 600,
            fontSize: 13.5,
            transition: "background-color .18s, box-shadow .18s, color .18s",
          },
          "& .Mui-selected": {
            color: "text.primary !important",
            bgcolor: isDark
              ? alpha(primary, 0.18)
              : alpha(primary, 0.10),
            backgroundImage: "linear-gradient(175deg,rgba(255,255,255,.20) 0%,rgba(255,255,255,0) 60%)",
            boxShadow: [
              isDark
                ? "inset 0 1px 0 rgba(255,255,255,.16)"
                : "inset 0 1px 0 rgba(255,255,255,.92)",
              `0 2px 10px -4px ${alpha(primary, isDark ? 0.45 : 0.30)}`,
            ].join(", "),
          },
          "& .MuiTab-root:not(.Mui-selected):hover": {
            bgcolor: isDark ? "rgba(255,255,255,.06)" : "rgba(255,255,255,.55)",
          },
        }}
      >
        {tabs.map((t, i) => (
          <Tab
            key={i}
            label={t.label}
            icon={t.icon as any}
            iconPosition="start"
          />
        ))}
      </Tabs>
    </Box>
  );
}
