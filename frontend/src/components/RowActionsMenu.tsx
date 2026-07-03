import { ReactNode, useState } from "react";
import {
  Button, IconButton, ListItemIcon, ListItemText, Menu, MenuItem, Stack, Tooltip,
} from "@mui/material";
import MoreVertIcon from "@mui/icons-material/esm/MoreVert";

export type ActionColor =
  | "inherit" | "primary" | "secondary" | "success" | "error" | "warning" | "info";

export interface RowAction {
  key: string;
  icon: ReactNode;
  label: string; // Persian; the menu-item text on mobile and the tooltip on desktop
  tooltip?: string; // longer desktop tooltip (defaults to label)
  onClick: () => void;
  disabled?: boolean;
  color?: ActionColor;
  /** Mobile: render as a labeled Button instead of a ⋮ menu entry. Ignored on desktop. */
  primary?: boolean;
}

/**
 * Desktop renderer: a compact Tooltip + small IconButton row — pixel-compatible with the
 * existing table action cells. Use inside the desktop `<Table>`.
 */
export function RowActionIcons({ actions }: { actions: RowAction[] }) {
  return (
    <Stack direction="row" spacing={0.3} justifyContent="flex-end" sx={{ flexWrap: "nowrap" }}>
      {actions.map((a) => (
        <Tooltip key={a.key} title={a.tooltip ?? a.label}>
          {/* span keeps the tooltip working while the button is disabled */}
          <span>
            <IconButton size="small" color={a.color ?? "default"} disabled={a.disabled} onClick={a.onClick}>
              {a.icon}
            </IconButton>
          </span>
        </Tooltip>
      ))}
    </Stack>
  );
}

/**
 * Mobile renderer: the `primary` actions become large labeled touch buttons; everything else
 * collapses behind one 40px ⋮ Menu with icon+Persian-label items — discoverable on touch (no
 * hover-only tooltips) and mis-tap-proof. The menu CLOSES before invoking `onClick`, so a
 * handler that opens `window.confirm()`/a dialog doesn't strand an open menu underneath.
 */
export default function RowActionsMenu({ actions }: { actions: RowAction[] }) {
  const [anchor, setAnchor] = useState<null | HTMLElement>(null);
  const primary = actions.filter((a) => a.primary);
  const overflow = actions.filter((a) => !a.primary);

  const run = (a: RowAction) => {
    setAnchor(null);
    a.onClick();
  };

  return (
    <Stack direction="row" spacing={0.75} alignItems="center" justifyContent="flex-end" sx={{ flexWrap: "wrap" }}>
      {primary.map((a) => (
        <Button
          key={a.key}
          size="small"
          variant="outlined"
          color={a.color ?? "primary"}
          disabled={a.disabled}
          onClick={a.onClick}
          startIcon={a.icon}
        >
          {a.label}
        </Button>
      ))}
      {overflow.length > 0 && (
        <>
          <IconButton
            size="small"
            aria-label="عملیات بیشتر"
            onClick={(e) => setAnchor(e.currentTarget)}
            sx={{ width: 40, height: 40 }}
          >
            <MoreVertIcon />
          </IconButton>
          <Menu anchorEl={anchor} open={Boolean(anchor)} onClose={() => setAnchor(null)}>
            {overflow.map((a) => (
              <MenuItem key={a.key} disabled={a.disabled} onClick={() => run(a)}>
                <ListItemIcon sx={{ color: a.color && a.color !== "inherit" ? `${a.color}.main` : undefined }}>
                  {a.icon}
                </ListItemIcon>
                <ListItemText>{a.label}</ListItemText>
              </MenuItem>
            ))}
          </Menu>
        </>
      )}
    </Stack>
  );
}
