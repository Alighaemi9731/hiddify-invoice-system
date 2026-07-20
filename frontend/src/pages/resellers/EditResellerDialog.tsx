import {
  Button,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  FormControlLabel,
  Stack,
  Switch,
  TextField,
} from "@mui/material";
import { ResellerRow } from "../../api/client";
import { useXsFullScreen } from "../../responsive";

export default function EditResellerDialog({
  form,
  onChange,
  onClose,
  onSave,
  saving,
}: {
  form: ResellerRow | null;
  onChange: (form: ResellerRow | null) => void;
  onClose: () => void;
  onSave: () => void;
  saving: boolean;
}) {
  const xsFull = useXsFullScreen();
  return (
    <Dialog open={!!form} onClose={onClose} fullWidth maxWidth="xs" fullScreen={xsFull}>
      <DialogTitle>ویرایش نماینده {form?.name}</DialogTitle>
      <DialogContent>
        <Stack spacing={2} sx={{ mt: 1 }}>
          <TextField
            label="قیمت هر گیگابایت (تومان) — خالی برای پیش‌فرض"
            type="number"
            value={form?.price_per_gb ?? ""}
            onChange={(event) => onChange(form ? {
              ...form,
              price_per_gb: event.target.value as any,
            } : null)}
          />
          <TextField
            label="حداقل فروش (تومان) — خالی برای پیش‌فرض، ۰ برای حذف حداقل"
            type="number"
            value={form?.min_sale_toman ?? ""}
            onChange={(event) => onChange(form ? {
              ...form,
              min_sale_toman: event.target.value as any,
            } : null)}
            helperText="برای کل مجموعهٔ این نماینده (خود نماینده و زیرمجموعه‌هایش) اعمال می‌شود."
          />
          <FormControlLabel
            control={
              <Switch
                checked={!!form?.exclude_from_billing}
                onChange={(event) => onChange(form ? {
                  ...form,
                  exclude_from_billing: event.target.checked,
                } : null)}
              />
            }
            label="معاف از صدور فاکتور"
          />
          <FormControlLabel
            control={
              <Switch
                checked={!!form?.storefront_enabled}
                onChange={(event) => onChange(form ? {
                  ...form,
                  storefront_enabled: event.target.checked,
                } : null)}
              />
            }
            label="ربات فروشگاهی (اجازهٔ راه‌اندازی)"
          />
          <TextField
            label="هزینهٔ ماهانهٔ ربات فروشگاهی (تومان) — خالی برای پیش‌فرض"
            type="number"
            value={form?.storefront_monthly_fee_toman ?? ""}
            onChange={(event) => onChange(form ? {
              ...form,
              storefront_monthly_fee_toman: event.target.value as any,
            } : null)}
            helperText="فقط در ماه‌هایی که نماینده ربات فروشگاهی فعال دارد، به فاکتور او افزوده می‌شود."
          />
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>انصراف</Button>
        <Button variant="contained" disabled={saving} onClick={onSave}>
          ذخیره
        </Button>
      </DialogActions>
    </Dialog>
  );
}
