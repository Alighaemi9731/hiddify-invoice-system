import {
  Button,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Stack,
  TextField,
  Typography,
} from "@mui/material";
import { ResellerRow } from "../../api/client";
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
  amount: number;
  onAmountChange: (amount: number) => void;
  onClose: () => void;
  onSubmit: (id: number, amount: number) => void;
  pending: boolean;
}) {
  const xsFull = useXsFullScreen();
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
                  variant={amount === preset ? "contained" : "outlined"}
                  onClick={() => onAmountChange(preset)}
                >
                  +{fmtNum(preset)}
                </Button>
              ))}
            </Stack>
            <TextField
              type="number"
              label="مقدار افزایش"
              fullWidth
              value={amount}
              onChange={(event) =>
                onAmountChange(Math.max(1, Number(event.target.value) || 0))}
            />
          </DialogContent>
          <DialogActions>
            <Button onClick={onClose}>انصراف</Button>
            <Button
              variant="contained"
              disabled={pending || amount < 1}
              onClick={() => onSubmit(row.id, amount)}
            >
              افزودن +{fmtNum(amount)}
            </Button>
          </DialogActions>
        </>
      )}
    </Dialog>
  );
}
