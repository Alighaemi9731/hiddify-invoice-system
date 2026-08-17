import {
  Button,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Stack,
  Typography,
} from "@mui/material";
import { ResellerRow } from "../../api/client";
import { NumberField, numberValue } from "../../components/NumberField";
import { fmtNum } from "../../format";
import { useXsFullScreen } from "../../responsive";

export default function BumpLimitsDialog({
  row,
  amount,
  onAmountChange,
  onClose,
  onSubmit,
  pending,
}: {
  row: ResellerRow | null;
  /** Raw text, so the field can be emptied while a new amount is typed. */
  amount: string;
  onAmountChange: (amount: string) => void;
  onClose: () => void;
  onSubmit: (id: number, amount: number) => void;
  pending: boolean;
}) {
  const xsFull = useXsFullScreen();
  const parsed = numberValue(amount);
  const valid = parsed !== null && parsed >= 1;
  return (
    <Dialog open={!!row} onClose={onClose} fullWidth maxWidth="xs" fullScreen={xsFull}>
      {row && (
        <>
          <DialogTitle>افزایش ظرفیت — {row.name}</DialogTitle>
          <DialogContent>
            <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
              این مقدار به هر دو سقف «تعداد کاربران» و «کاربران فعال» این نماینده روی پنل افزوده می‌شود.
              {row.panel_max_users != null && (
                <> سقف فعلی: {fmtNum(row.panel_max_users)} (ساخته‌شده: {fmtNum(row.users_count)}).</>
              )}
            </Typography>
            <Stack direction="row" spacing={1} sx={{ mb: 2 }}>
              {[50, 100, 200, 500].map((preset) => (
                <Button
                  key={preset}
                  size="small"
                  variant={parsed === preset ? "contained" : "outlined"}
                  onClick={() => onAmountChange(String(preset))}
                >
                  +{fmtNum(preset)}
                </Button>
              ))}
            </Stack>
            <NumberField
              label="مقدار افزایش"
              fullWidth
              value={amount}
              error={amount !== "" && !valid}
              onChange={onAmountChange}
            />
          </DialogContent>
          <DialogActions>
            <Button onClick={onClose}>انصراف</Button>
            <Button
              variant="contained"
              disabled={pending || !valid}
              onClick={() => valid && onSubmit(row.id, parsed)}
            >
              افزودن +{fmtNum(parsed ?? 0)}
            </Button>
          </DialogActions>
        </>
      )}
    </Dialog>
  );
}
