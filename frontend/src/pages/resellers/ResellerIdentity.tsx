import { MouseEvent, ReactNode } from "react";
import { Box, IconButton, Stack, Tooltip, Typography } from "@mui/material";
import { alpha } from "@mui/material/styles";
import KeyboardArrowDownIcon from "@mui/icons-material/esm/KeyboardArrowDown";
import KeyboardArrowLeftIcon from "@mui/icons-material/esm/KeyboardArrowLeft";
import WarningAmberIcon from "@mui/icons-material/esm/WarningAmber";
import { ResellerRow, ResellerTreeRow } from "../../api/client";

function StatusPill({
  children,
  color,
  muted = false,
}: {
  children: ReactNode;
  color: string;
  muted?: boolean;
}) {
  return (
    <Box
      component="span"
      sx={{
        display: "inline-flex",
        alignItems: "center",
        gap: 0.7,
        px: 1.1,
        py: 0.55,
        borderRadius: 99,
        color: muted ? "text.secondary" : color,
        bgcolor: (theme) => alpha(color, muted ? 0.05 : theme.palette.mode === "dark" ? 0.16 : 0.09),
        border: "1px solid",
        borderColor: (theme) => alpha(color, muted ? 0.12 : theme.palette.mode === "dark" ? 0.34 : 0.22),
        fontSize: 12,
        fontWeight: 750,
        lineHeight: 1,
        whiteSpace: "nowrap",
      }}
    >
      <Box sx={{ width: 7, height: 7, borderRadius: "50%", bgcolor: "currentColor" }} />
      {children}
    </Box>
  );
}

export function ConnectionStatus({ connected }: { connected: boolean }) {
  return connected ? (
    <StatusPill color="#10b981">متصل</StatusPill>
  ) : (
    <StatusPill color="#94a3b8" muted>متصل نیست</StatusPill>
  );
}

export function EnforcementStatus({ state }: { state: string }) {
  if (state === "enforced") return <StatusPill color="#f43f5e">مسدود</StatusPill>;
  if (state === "frozen") return <StatusPill color="#f59e0b">محدود</StatusPill>;
  return <StatusPill color="#10b981">فعال</StatusPill>;
}

export function ResellerIdentity({
  reseller,
  depth = 0,
  tree = false,
  expanded = false,
  onToggle,
}: {
  reseller: ResellerRow | ResellerTreeRow;
  depth?: number;
  tree?: boolean;
  expanded?: boolean;
  onToggle?: () => void;
}) {
  const treeRow = reseller as ResellerTreeRow;
  const hasChildren = tree && (treeRow.children?.length || 0) > 0;

  return (
    <Box
      sx={{
        position: "relative",
        display: "flex",
        alignItems: "center",
        minHeight: 43,
        ps: tree ? depth * 3.1 : 0,
      }}
    >
      {tree && depth > 0 && (
        <>
          <Box
            sx={{
              position: "absolute",
              insetInlineStart: (depth - 1) * 24 + 10,
              top: -15,
              bottom: "50%",
              width: 18,
              borderInlineStart: "1px solid",
              borderBottom: "1px solid",
              borderColor: "divider",
              borderEndStartRadius: 8,
            }}
          />
          <Box
            sx={{
              position: "absolute",
              insetInlineStart: (depth - 1) * 24 + 10,
              top: "50%",
              bottom: -16,
              borderInlineStart: "1px solid",
              borderColor: "divider",
            }}
          />
        </>
      )}
      {tree && (
        <Box sx={{ width: 34, flexShrink: 0, display: "grid", placeItems: "center" }}>
          {hasChildren ? (
            <IconButton
              size="small"
              aria-label={expanded ? "بستن زیرمجموعه‌ها" : "باز کردن زیرمجموعه‌ها"}
              onClick={(event: MouseEvent) => {
                event.stopPropagation();
                onToggle?.();
              }}
              sx={{
                width: 28,
                height: 28,
                bgcolor: (theme) => alpha(theme.palette.primary.main, 0.09),
              }}
            >
              {expanded
                ? <KeyboardArrowDownIcon fontSize="small" />
                : <KeyboardArrowLeftIcon fontSize="small" />}
            </IconButton>
          ) : (
            <Box
              sx={{
                width: 7,
                height: 7,
                borderRadius: "50%",
                bgcolor: depth === 0 ? "primary.main" : "text.disabled",
              }}
            />
          )}
        </Box>
      )}
      <Box sx={{ minWidth: 0 }}>
        <Stack direction="row" alignItems="center" spacing={0.8}>
          <Typography
            variant="body2"
            noWrap
            sx={{ fontWeight: depth === 0 ? 800 : 600, maxWidth: 220 }}
          >
            {reseller.name || "بدون نام"}
          </Typography>
          {treeRow.cycle_detected && (
            <Tooltip title="ساختار والد/فرزند این شاخه نامعتبر است">
              <WarningAmberIcon color="warning" sx={{ fontSize: 17 }} />
            </Tooltip>
          )}
        </Stack>
      </Box>
    </Box>
  );
}
