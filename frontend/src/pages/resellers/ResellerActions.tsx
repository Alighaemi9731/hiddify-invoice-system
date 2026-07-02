import { IconButton, Stack, Switch, Tooltip, Typography } from "@mui/material";
import AddIcon from "@mui/icons-material/esm/Add";
import BlockIcon from "@mui/icons-material/esm/Block";
import EditIcon from "@mui/icons-material/esm/Edit";
import RestartAltIcon from "@mui/icons-material/esm/RestartAlt";
import { ResellerRow } from "../../api/client";

/** The slice of a react-query mutation the row controls actually use. */
type MutationLike<TVars> = { isPending: boolean; mutate: (vars: TVars) => void };

export function ResellerActions({
  reseller,
  onEdit,
  onBump,
  enforce,
  restore,
}: {
  reseller: ResellerRow;
  onEdit: (reseller: ResellerRow) => void;
  onBump: (reseller: ResellerRow) => void;
  enforce: MutationLike<number>;
  restore: MutationLike<number>;
}) {
  return (
    <Stack direction="row" spacing={0.2} justifyContent="flex-end">
      <Tooltip title="ویرایش">
        <IconButton size="small" onClick={() => onEdit(reseller)}>
          <EditIcon fontSize="small" />
        </IconButton>
      </Tooltip>
      <Tooltip title="افزایش ظرفیت کاربران">
        <IconButton
          size="small"
          color="primary"
          onClick={() => onBump(reseller)}
        >
          <AddIcon fontSize="small" />
        </IconButton>
      </Tooltip>
      {reseller.enforcement_state === "enforced" ? (
        <Tooltip title="بازگردانی">
          <IconButton
            size="small"
            color="success"
            disabled={restore.isPending}
            onClick={() => restore.mutate(reseller.id)}
          >
            <RestartAltIcon fontSize="small" />
          </IconButton>
        </Tooltip>
      ) : (
        <Tooltip title="مسدودسازی">
          <IconButton
            size="small"
            color="error"
            disabled={enforce.isPending}
            onClick={() => {
              if (confirm("مسدودسازی این نماینده؟ (در حالت آزمایشی فقط ثبت می‌شود)")) {
                enforce.mutate(reseller.id);
              }
            }}
          >
            <BlockIcon fontSize="small" />
          </IconButton>
        </Tooltip>
      )}
    </Stack>
  );
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
