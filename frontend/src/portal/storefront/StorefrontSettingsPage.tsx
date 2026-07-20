import { FormEvent, useEffect, useRef, useState } from "react";
import {
  Alert, Box, Button, Chip, FormControlLabel, Grid, Stack, Switch, TextField, Typography,
} from "@mui/material";
import DeleteOutlineIcon from "@mui/icons-material/esm/DeleteOutline";
import SaveIcon from "@mui/icons-material/esm/Save";
import VerifiedIcon from "@mui/icons-material/esm/Verified";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useOutletContext } from "react-router-dom";
import { DataState } from "../../components/DataState";
import { SectionCard } from "../ui";
import {
  deleteStorefrontChannel, getStorefrontSettings, saveStorefrontChannel,
  setStorefrontChannelEnabled, storefrontQueryKeys, updateStorefrontSettings,
} from "./api";
import StorefrontConflictDialog from "./StorefrontConflictDialog";
import type { StorefrontOutletContext } from "./StorefrontShell";
import type {
  StorefrontMessageSettings, StorefrontPaymentSettings, StorefrontSettings,
  StorefrontSettingsByGroup, StorefrontSettingsGroup, StorefrontShopStateSettings,
  StorefrontTrialSettings, Versioned,
} from "./types";
import { commandRecoveryMessage, isVersionConflict, useIdempotentMutation } from "./mutation";

type SettingsCommand =
  | { type: "settings"; group: StorefrontSettingsGroup; body: Partial<StorefrontSettingsByGroup[StorefrontSettingsGroup]>; etag?: string }
  | { type: "channel-save"; channelId: string; etag?: string }
  | { type: "channel-enabled"; enabled: boolean; etag?: string }
  | { type: "channel-delete"; etag?: string };

const translateCardDigits = (value: string) => value
  .replace(/[۰-۹]/g, (digit) => String("۰۱۲۳۴۵۶۷۸۹".indexOf(digit)))
  .replace(/[٠-٩]/g, (digit) => String("٠١٢٣٤٥٦٧٨٩".indexOf(digit)));

const cardDigits = (value: string) => {
  const translated = translateCardDigits(value);
  if (!/^[0-9 _\-\u200c]*$/.test(translated)) return null;
  return translated.replace(/[ _\-\u200c]/g, "");
};

export default function StorefrontSettingsPage() {
  const { shop } = useOutletContext<StorefrontOutletContext>();
  const queryClient = useQueryClient();
  const queryKey = storefrontQueryKeys.settings(shop.id);
  const query = useQuery({ queryKey, queryFn: () => getStorefrontSettings(shop.id) });
  const [payment, setPayment] = useState<StorefrontPaymentSettings | null>(null);
  const [trial, setTrial] = useState<StorefrontTrialSettings | null>(null);
  const [messages, setMessages] = useState<StorefrontMessageSettings | null>(null);
  const [shopState, setShopState] = useState<StorefrontShopStateSettings | null>(null);
  const [channelId, setChannelId] = useState("");
  const [conflict, setConflict] = useState<SettingsCommand | null>(null);
  const [conflictReloadError, setConflictReloadError] = useState(false);
  const [saved, setSaved] = useState(false);
  const priorServer = useRef<StorefrontSettings | null>(null);
  const resetAfterSave = useRef<Set<string>>(new Set());

  useEffect(() => {
    if (!query.data) return;
    const next = query.data.data;
    const previous = priorServer.current;
    const resets = new Set(resetAfterSave.current);
    setPayment((draft) => shouldReplaceDraft(previous?.payment, draft, resets.has("payment")) ? next.payment : draft);
    setTrial((draft) => shouldReplaceDraft(previous?.trial, draft, resets.has("trial")) ? next.trial : draft);
    setMessages((draft) => shouldReplaceDraft(previous?.messages, draft, resets.has("messages")) ? next.messages : draft);
    setShopState((draft) => shouldReplaceDraft(previous?.shop_state, draft, resets.has("shop-state")) ? next.shop_state : draft);
    setChannelId((draft) => !previous || resets.has("channel") || draft === (previous.channel.channel_id || "")
      ? next.channel.channel_id || ""
      : draft);
    priorServer.current = next;
    resetAfterSave.current.clear();
  }, [query.data?.data]);

  const mutation = useIdempotentMutation<Versioned<unknown>, SettingsCommand>(
    async (input, key) => {
      const etag = input.etag || query.data?.etag || "";
      if (!etag) throw new Error("missing storefront config version");
      if (input.type === "settings") {
        return updateStorefrontSettings(shop.id, input.group, input.body, etag, key);
      }
      if (input.type === "channel-save") return saveStorefrontChannel(shop.id, input.channelId, etag, key);
      if (input.type === "channel-enabled") return setStorefrontChannelEnabled(shop.id, input.enabled, etag, key);
      return deleteStorefrontChannel(shop.id, etag, key);
    },
    {
      onSuccess: async (result, variables) => {
        resetAfterSave.current.add(variables.type === "settings" ? variables.group : "channel");
        if (result.etag) {
          queryClient.setQueryData<Versioned<StorefrontSettings>>(queryKey, (current) =>
            current ? { ...current, etag: result.etag } : current);
        }
        const refreshes: Array<Promise<unknown>> = [query.refetch()];
        refreshes.push(queryClient.invalidateQueries({ queryKey: storefrontQueryKeys.preview(shop.id), exact: true }));
        if (variables.type === "settings" && variables.group === "shop-state") {
          refreshes.push(queryClient.invalidateQueries({ queryKey: storefrontQueryKeys.all, exact: true }));
        }
        await Promise.all(refreshes);
        setSaved(true);
      },
      onError: (error, variables) => {
        if (isVersionConflict(error)) setConflict(variables);
      },
    },
  );

  const saveGroup = <G extends StorefrontSettingsGroup>(group: G, body: StorefrontSettingsByGroup[G]) => {
    setSaved(false);
    const base = (
      group === "shop-state"
        ? query.data?.data.shop_state
        : query.data?.data[group as Exclude<StorefrontSettingsGroup, "shop-state">]
    ) as StorefrontSettingsByGroup[G] | undefined;
    const changes = base ? changedFields(base as StorefrontSettingsByGroup[G], body) : body;
    if (Object.keys(changes).length) {
      mutation.mutate({ type: "settings", group, body: changes } as SettingsCommand);
    }
  };
  const paymentValid = !!payment && (
    !payment.pay_card_enabled
      || (cardDigits(payment.card_number || "")?.length === 16
        && (payment.card_holder?.trim().length || 0) >= 2
        && (payment.card_holder?.trim().length || 0) <= 128)
  ) && (!payment.pay_usdt_enabled || validUsdtAddress(payment.usdt_address))
    && (!payment.pay_ton_enabled || validTonAddress(payment.ton_address));
  const trialValid = !!trial && trial.free_trial_gb >= 1 && trial.free_trial_gb <= 1000
    && trial.free_trial_days >= 1 && trial.free_trial_days <= 90;
  const messagesValid = !!messages && (messages.welcome_text?.length || 0) <= 1000
    && (messages.support_contact?.length || 0) <= 128;
  const shopStateValid = !!shopState && (shopState.closed_text?.length || 0) <= 1000;
  const channelValid = /^@[A-Za-z0-9_]{5,32}$/.test(channelId) || /^-?[1-9][0-9]{0,19}$/.test(channelId);
  const paymentForSave = payment ? cleanPayment(payment) : null;
  const paymentDirty = !!paymentForSave && !!query.data
    && Object.keys(changedFields(query.data.data.payment, paymentForSave)).length > 0;
  const trialDirty = !!trial && !!query.data && Object.keys(changedFields(query.data.data.trial, trial)).length > 0;
  const messagesDirty = !!messages && !!query.data && Object.keys(changedFields(query.data.data.messages, messages)).length > 0;
  const shopStateDirty = !!shopState && !!query.data && Object.keys(changedFields(query.data.data.shop_state, shopState)).length > 0;

  return (
    <Box>
      <Stack spacing={0.5} sx={{ mb: 2 }}>
        <Typography variant="h6">تنظیمات فروشگاه</Typography>
        <Typography variant="body2" color="text.secondary">پرداخت، تست رایگان، پیام‌ها و وضعیت فروشگاه را مدیریت کنید.</Typography>
      </Stack>
      {saved && <Alert severity="success" onClose={() => setSaved(false)} sx={{ mb: 2 }}>تنظیمات ذخیره شد.</Alert>}
      {mutation.isError && !isVersionConflict(mutation.error) && (
        <Alert severity="error" sx={{ mb: 2 }}>
          {commandRecoveryMessage(mutation.error) || "ذخیره انجام نشد. ورودی‌ها یا اتصال را بررسی کنید."}
        </Alert>
      )}
      <DataState isLoading={query.isLoading} isError={query.isError} rows={8} onRetry={() => query.refetch()}>
        {query.data && payment && trial && messages && shopState && (
          <Box component="fieldset" disabled={mutation.isPending} sx={{ border: 0, p: 0, m: 0, minWidth: 0 }}>
          <Grid container spacing={2}>
            <Grid item xs={12} lg={6}>
              <SectionCard title="روش‌های پرداخت">
                <Box component="form" onSubmit={(event: FormEvent) => {
                  event.preventDefault();
                  if (paymentValid && paymentForSave) saveGroup("payment", paymentForSave);
                }}>
                  <Stack spacing={1.5}>
                    <FormControlLabel control={<Switch checked={payment.pay_card_enabled} onChange={(_, value) => setPayment({ ...payment, pay_card_enabled: value })} />} label="کارت به کارت" />
                    {payment.pay_card_enabled && <>
                      <TextField label="شماره کارت" value={payment.card_number || ""} error={!!payment.card_number && cardDigits(payment.card_number)?.length !== 16} helperText="۱۶ رقم؛ فقط رقم، فاصله یا خط تیره" onChange={(event) => setPayment({ ...payment, card_number: translateCardDigits(event.target.value).slice(0, 32) })} inputProps={{ inputMode: "numeric" }} />
                      <TextField label="نام صاحب کارت" value={payment.card_holder || ""} inputProps={{ maxLength: 128 }} onChange={(event) => setPayment({ ...payment, card_holder: event.target.value })} />
                    </>}
                    <FormControlLabel control={<Switch checked={payment.pay_usdt_enabled} onChange={(_, value) => setPayment({ ...payment, pay_usdt_enabled: value })} />} label="USDT" />
                    {payment.pay_usdt_enabled && <TextField label="آدرس USDT" value={payment.usdt_address || ""} error={!!payment.usdt_address && !validUsdtAddress(payment.usdt_address)} helperText="آدرس BEP-20 با 0x و ۴۰ رقم هگز" inputProps={{ maxLength: 128, dir: "ltr" }} onChange={(event) => setPayment({ ...payment, usdt_address: event.target.value })} />}
                    <FormControlLabel control={<Switch checked={payment.pay_ton_enabled} onChange={(_, value) => setPayment({ ...payment, pay_ton_enabled: value })} />} label="TON" />
                    {payment.pay_ton_enabled && <TextField label="آدرس TON" value={payment.ton_address || ""} error={!!payment.ton_address && !validTonAddress(payment.ton_address)} helperText="آدرسِ کاربرپسند (friendly) شبکهٔ TON" inputProps={{ maxLength: 128, dir: "ltr" }} onChange={(event) => setPayment({ ...payment, ton_address: event.target.value })} />}
                    <SaveButton disabled={!paymentValid || !paymentDirty || mutation.isPending} />
                  </Stack>
                </Box>
              </SectionCard>
            </Grid>

            <Grid item xs={12} lg={6}>
              <SectionCard title="تست رایگان">
                <Box component="form" onSubmit={(event: FormEvent) => { event.preventDefault(); if (trialValid) saveGroup("trial", trial); }}>
                  <Stack spacing={1.5}>
                    <FormControlLabel control={<Switch checked={trial.free_trial_enabled} onChange={(_, value) => setTrial({ ...trial, free_trial_enabled: value })} />} label="تست رایگان فعال باشد" />
                    <TextField label="حجم (گیگابایت)" type="number" value={trial.free_trial_gb} error={trial.free_trial_gb < 1 || trial.free_trial_gb > 1000} inputProps={{ min: 1, max: 1000 }} onChange={(event) => setTrial({ ...trial, free_trial_gb: Number(event.target.value) })} />
                    <TextField label="مدت (روز)" type="number" value={trial.free_trial_days} error={trial.free_trial_days < 1 || trial.free_trial_days > 90} inputProps={{ min: 1, max: 90 }} onChange={(event) => setTrial({ ...trial, free_trial_days: Number(event.target.value) })} />
                    <SaveButton disabled={!trialValid || !trialDirty || mutation.isPending} />
                  </Stack>
                </Box>
              </SectionCard>
            </Grid>

            <Grid item xs={12} lg={6}>
              <SectionCard title="پیام‌ها و پشتیبانی">
                <Box component="form" onSubmit={(event: FormEvent) => { event.preventDefault(); if (messagesValid) saveGroup("messages", messages); }}>
                  <Stack spacing={1.5}>
                    <TextField multiline minRows={4} label="پیام خوش‌آمد" value={messages.welcome_text || ""} error={(messages.welcome_text?.length || 0) > 1000} helperText={`${messages.welcome_text?.length || 0}/1000`} onChange={(event) => setMessages({ ...messages, welcome_text: event.target.value })} />
                    <TextField label="راه ارتباطی پشتیبانی" value={messages.support_contact || ""} inputProps={{ maxLength: 128 }} onChange={(event) => setMessages({ ...messages, support_contact: event.target.value })} />
                    <SaveButton disabled={!messagesValid || !messagesDirty || mutation.isPending} />
                  </Stack>
                </Box>
              </SectionCard>
            </Grid>

            <Grid item xs={12} lg={6}>
              <SectionCard title="وضعیت فروشگاه">
                <Box component="form" onSubmit={(event: FormEvent) => {
                  event.preventDefault();
                  if (!shopStateValid) return;
                  if (shopState.shop_closed && !query.data.data.shop_state.shop_closed
                    && !window.confirm("فروشگاه بسته شود؟ خرید و تمدید مشتریان متوقف خواهد شد.")) return;
                  saveGroup("shop-state", shopState);
                }}>
                  <Stack spacing={1.5}>
                    <FormControlLabel control={<Switch checked={shopState.shop_closed} onChange={(_, value) => setShopState({ ...shopState, shop_closed: value })} />} label={shopState.shop_closed ? "فروشگاه بسته است" : "فروشگاه باز است"} />
                    <TextField multiline minRows={3} label="پیام زمان بسته بودن" value={shopState.closed_text || ""} error={(shopState.closed_text?.length || 0) > 1000} helperText={`${shopState.closed_text?.length || 0}/1000`} onChange={(event) => setShopState({ ...shopState, closed_text: event.target.value })} />
                    <SaveButton disabled={!shopStateValid || !shopStateDirty || mutation.isPending} />
                  </Stack>
                </Box>
              </SectionCard>
            </Grid>

            <Grid item xs={12}>
              <SectionCard
                title="عضویت اجباری در کانال"
                action={<Chip size="small" icon={query.data.data.channel.verified ? <VerifiedIcon /> : undefined} color={query.data.data.channel.health === "ok" ? "success" : query.data.data.channel.health === "error" ? "error" : "default"} label={query.data.data.channel.verified ? "آخرین وضعیت ثبت‌شده: تأییدشده" : "آخرین وضعیت ثبت‌شده: غیرفعال یا ناموفق"} />}
              >
                <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 1.5 }}>
                  این وضعیت نتیجهٔ آخرین بررسی هنگام ذخیره است و بررسی زنده محسوب نمی‌شود.
                </Typography>
                <Stack direction={{ xs: "column", md: "row" }} spacing={1.5} alignItems={{ md: "center" }}>
                  <TextField fullWidth label="شناسه یا نام کاربری کانال" value={channelId} error={!!channelId && !channelValid} helperText="@username با ۵ تا ۳۲ نویسه، یا شناسهٔ عددی غیرصفر" inputProps={{ maxLength: 64, dir: "ltr" }} placeholder="@channel یا -100…" onChange={(event) => setChannelId(event.target.value.trim())} />
                  <Button variant="contained" disabled={!channelValid || mutation.isPending} onClick={() => mutation.mutate({ type: "channel-save", channelId })}>بررسی و ذخیره</Button>
                  {query.data.data.channel.channel_id && <>
                    <FormControlLabel control={<Switch checked={query.data.data.channel.channel_required} disabled={mutation.isPending} onChange={(_, enabled) => mutation.mutate({ type: "channel-enabled", enabled })} />} label="اجباری" />
                    <Button color="error" startIcon={<DeleteOutlineIcon />} disabled={mutation.isPending} onClick={() => window.confirm("تنظیم کانال حذف شود؟") && mutation.mutate({ type: "channel-delete" })}>حذف</Button>
                  </>}
                </Stack>
                {query.data.data.channel.channel_link && (
                  <Typography variant="caption" color="text.secondary" dir="ltr" sx={{ display: "block", mt: 1.5, textAlign: "right" }}>
                    {query.data.data.channel.channel_link}
                  </Typography>
                )}
              </SectionCard>
            </Grid>
          </Grid>
          </Box>
        )}
      </DataState>

      <StorefrontConflictDialog
        open={!!conflict}
        busy={query.isFetching || mutation.isPending}
        reloadError={conflictReloadError}
        onClose={() => { setConflict(null); setConflictReloadError(false); }}
        onReload={() => {
          const resetKey = conflict?.type === "settings" ? conflict.group : "channel";
          resetAfterSave.current.add(resetKey);
          void query.refetch().then((fresh) => {
            if (fresh.isSuccess) {
              setConflict(null);
              setConflictReloadError(false);
            } else {
              resetAfterSave.current.delete(resetKey);
              setConflictReloadError(true);
            }
          });
        }}
        onReapply={() => void query.refetch().then((fresh) => {
          if (conflict && fresh.isSuccess && fresh.data?.etag) {
            mutation.mutate({ ...conflict, etag: fresh.data.etag });
            setConflict(null);
            setConflictReloadError(false);
          } else setConflictReloadError(true);
        })}
      />
    </Box>
  );
}

function SaveButton({ disabled }: { disabled: boolean }) {
  return <Button type="submit" variant="contained" startIcon={<SaveIcon />} disabled={disabled} sx={{ alignSelf: "flex-start" }}>ذخیره</Button>;
}

const validUsdtAddress = (value: string | null) => /^0x[0-9a-fA-F]{40}$/.test(value?.trim() || "");
const validTonAddress = (value: string | null) => /^(?:EQ|UQ|kQ|0Q)[A-Za-z0-9_-]{46}$/.test(value?.trim() || "");

function changedFields<T extends object>(before: T, after: T): Partial<T> {
  return Object.fromEntries(
    (Object.keys(after) as Array<keyof T>)
      .filter((key) => before[key] !== after[key])
      .map((key) => [key, after[key]]),
  ) as Partial<T>;
}

function shouldReplaceDraft<T extends object>(server: T | undefined, draft: T | null, force: boolean) {
  return force || !server || !draft || Object.keys(changedFields(server, draft)).length === 0;
}

function cleanPayment(value: StorefrontPaymentSettings): StorefrontPaymentSettings {
  return {
    ...value,
    card_number: cardDigits(value.card_number || "") || null,
    card_holder: value.card_holder?.trim() || null,
    usdt_address: value.usdt_address?.trim() || null,
    ton_address: value.ton_address?.trim() || null,
  };
}
