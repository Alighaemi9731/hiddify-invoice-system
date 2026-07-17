import { Alert, Button, Dialog, DialogActions, DialogContent, DialogTitle, Typography } from "@mui/material";

export default function StorefrontConflictDialog({
  open,
  busy,
  reloadError,
  onClose,
  onReload,
  onReapply,
}: {
  open: boolean;
  busy?: boolean;
  reloadError?: boolean;
  onClose: () => void;
  onReload: () => void;
  onReapply: () => void;
}) {
  return (
    <Dialog open={open} onClose={busy ? undefined : onClose} maxWidth="xs" fullWidth>
      <DialogTitle>اطلاعات فروشگاه تغییر کرده است</DialogTitle>
      <DialogContent>
        <Typography variant="body2" color="text.secondary">
          مدیر دیگری این بخش را به‌روزرسانی کرده است. نسخهٔ تازه را بارگذاری کنید یا پس از
          بارگذاری، تغییر فعلی خود را دوباره اعمال کنید.
        </Typography>
        {reloadError && (
          <Alert severity="error" sx={{ mt: 2 }}>
            نسخهٔ تازه بارگذاری نشد. اتصال را بررسی کنید و دوباره تلاش کنید.
          </Alert>
        )}
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} disabled={busy}>انصراف</Button>
        <Button onClick={onReload} disabled={busy}>بارگذاری نسخهٔ جدید</Button>
        <Button variant="contained" onClick={onReapply} disabled={busy}>بارگذاری و اعمال دوباره</Button>
      </DialogActions>
    </Dialog>
  );
}
