import { useEffect, useState } from "react";
import {
  Alert, Button, Chip, Dialog, DialogActions, DialogContent, DialogTitle, FormControlLabel,
  Stack, Switch, TextField, Typography,
} from "@mui/material";
import { fmtNum } from "../../format";
import { useXsFullScreen } from "../../responsive";

const SNOOZE_CHOICES = [0, 7, 15, 30, 60];

export interface FollowupDraft {
  ids: number[];
  title: string;
  /** Pre-fills the pinned note when following up on exactly one reseller. */
  pinnedNote?: string;
}

/**
 * "I followed this one up." The system never sends the message — the owner writes to the
 * reseller in Telegram themselves and records it here, which snoozes the row so the same
 * person does not resurface on the next pass.
 */
export default function FollowupDialog({
  draft, defaultSnoozeDays, busy, onClose, onSubmit,
}: {
  draft: FollowupDraft | null;
  defaultSnoozeDays: number;
  busy: boolean;
  onClose: () => void;
  onSubmit: (body: {
    note: string; snooze_days: number; muted: boolean; pinned_note?: string;
  }) => void;
}) {
  const fullScreen = useXsFullScreen();
  const [note, setNote] = useState("");
  const [pinned, setPinned] = useState("");
  const [days, setDays] = useState(defaultSnoozeDays);
  const [muted, setMuted] = useState(false);
  const single = draft?.ids.length === 1;

  useEffect(() => {
    if (draft) {
      setNote("");
      setPinned(draft.pinnedNote || "");
      setDays(defaultSnoozeDays);
      setMuted(false);
    }
  }, [draft, defaultSnoozeDays]);

  return (
    <Dialog open={!!draft} onClose={onClose} fullWidth maxWidth="sm" fullScreen={fullScreen}>
      <DialogTitle>ثبت پیگیری — {draft?.title}</DialogTitle>
      <DialogContent>
        <Stack spacing={2.5} sx={{ mt: 1 }}>
          <Alert severity="info">
            این فرم هیچ پیامی نمی‌فرستد. خودتان در تلگرام پیام بدهید و بعد اینجا ثبت کنید تا
            تا پایان مهلت تعویق دوباره در فهرست کاری نیاید.
          </Alert>
          <TextField
            label="یادداشت این پیگیری" value={note} onChange={(e) => setNote(e.target.value)}
            multiline minRows={2} fullWidth size="small"
            helperText="مثلاً: زنگ زدم، گفت این ماه سفر است و از ماه بعد دوباره شروع می‌کند."
          />
          <div>
            <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
              تا چند روز از فهرست کاری کنار گذاشته شود؟
            </Typography>
            <Stack direction="row" spacing={1} useFlexGap flexWrap="wrap">
              {SNOOZE_CHOICES.map((d) => (
                <Chip
                  key={d} size="small" onClick={() => { setDays(d); setMuted(false); }}
                  color={!muted && days === d ? "primary" : "default"}
                  variant={!muted && days === d ? "filled" : "outlined"}
                  label={d === 0 ? "بدون تعویق" : `${fmtNum(d)} روز`}
                />
              ))}
            </Stack>
          </div>
          {single && (
            <TextField
              label="یادداشت ثابت این نماینده" value={pinned}
              onChange={(e) => setPinned(e.target.value)} multiline minRows={2} fullWidth
              size="small"
              helperText="روی کارت نماینده می‌ماند و با همگام‌سازی پنل پاک نمی‌شود."
            />
          )}
          <FormControlLabel
            control={<Switch checked={muted} onChange={(e) => setMuted(e.target.checked)} />}
            label="دیگر هرگز در فهرست کاری نیاور"
          />
          {muted && (
            <Alert severity="warning">
              این نماینده تا وقتی خودتان «برگرداندن به فهرست» را نزنید در هیچ نمایی نمی‌آید.
            </Alert>
          )}
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} disabled={busy}>انصراف</Button>
        <Button
          variant="contained" disabled={busy}
          onClick={() => onSubmit({
            note, snooze_days: days, muted,
            ...(single ? { pinned_note: pinned } : {}),
          })}
        >
          ثبت پیگیری
        </Button>
      </DialogActions>
    </Dialog>
  );
}
