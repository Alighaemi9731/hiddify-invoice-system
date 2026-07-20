import { Stack, Switch, Tooltip, Typography } from "@mui/material";
import AddIcon from "@mui/icons-material/esm/Add";
import BlockIcon from "@mui/icons-material/esm/Block";
import EditIcon from "@mui/icons-material/esm/Edit";
import RestartAltIcon from "@mui/icons-material/esm/RestartAlt";
import { ResellerRow } from "../../api/client";
import RowActionsMenu, { RowActionIcons, RowAction } from "../../components/RowActionsMenu";

/** The slice of a react-query mutation the row controls actually use. */
type MutationLike<TVars> = { isPending: boolean; mutate: (vars: TVars) => void };

interface ActionArgs {
  reseller: ResellerRow;
  onEdit: (reseller: ResellerRow) => void;
  onBump: (reseller: ResellerRow) => void;
  enforce: MutationLike<number>;
  restore: MutationLike<number>;
}

/** Single source of truth: «ویرایش» is the touch-first primary; capacity + suspend/restore
 *  fold into the ⋮ menu on mobile and stay an icon row on desktop. */
function resellerActions({ reseller, onEdit, onBump, enforce, restore }: ActionArgs): RowAction[] {
  const acts: RowAction[] = [
    { key: "edit", label: "ویرایش", icon: <EditIcon fontSize="small" />, onClick: () => onEdit(reseller), primary: true },
    { key: "bump", label: "افزایش ظرفیت کاربران", color: "primary", icon: <AddIcon fontSize="small" />, onClick: () => onBump(reseller) },
  ];
  if (reseller.enforcement_state === "enforced")
    acts.push({ key: "restore", label: "بازگردانی", color: "success", icon: <RestartAltIcon fontSize="small" />, disabled: restore.isPending, onClick: () => restore.mutate(reseller.id) });
  else
    acts.push({ key: "enforce", label: "مسدودسازی", color: "error", icon: <BlockIcon fontSize="small" />, disabled: enforce.isPending, onClick: () => { if (confirm("این نماینده مسدود شود؟ (در حالت آزمایشی فقط ثبت می‌شود)")) enforce.mutate(reseller.id); } });
  return acts;
}

/** Desktop: icon row (table cell). */
export function ResellerActions(args: ActionArgs) {
  return <RowActionIcons actions={resellerActions(args)} />;
}

/** Mobile: labeled primary button + ⋮ menu (card footer). */
export function ResellerActionsMobile(args: ActionArgs) {
  return <RowActionsMenu actions={resellerActions(args)} />;
}

export function CanAddSwitch({
  reseller,
  canAdd,
}: {
  reseller: ResellerRow;
  canAdd: MutationLike<{ id: number; enabled: boolean }>;
}) {
  return (
    <Tooltip
      title={reseller.can_add_admin
        ? "اجازهٔ ساخت زیرمجموعه دارد"
        : "اجازهٔ ساخت زیرمجموعه ندارد"}
    >
      <Stack direction="row" alignItems="center" spacing={0.5}>
        <Switch
          size="small"
          checked={!!reseller.can_add_admin}
          disabled={canAdd.isPending}
          onChange={(event) =>
            canAdd.mutate({ id: reseller.id, enabled: event.target.checked })}
        />
        <Typography variant="caption" color="text.secondary">
          {reseller.can_add_admin ? "فعال" : "غیرفعال"}
        </Typography>
      </Stack>
    </Tooltip>
  );
}
