import { useState } from "react";
import { Alert, Box, Button, Card, CardContent, IconButton, Stack, TextField, Typography } from "@mui/material";
import DeleteOutlineIcon from "@mui/icons-material/esm/DeleteOutline";
import PersonAddIcon from "@mui/icons-material/esm/PersonAdd";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useOutletContext } from "react-router-dom";
import { DataState } from "../../components/DataState";
import { fmtNum } from "../../format";
import { addStorefrontManager, listStorefrontManagers, removeStorefrontManager, storefrontQueryKeys } from "./api";
import StorefrontConflictDialog from "./StorefrontConflictDialog";
import type { StorefrontOutletContext } from "./StorefrontShell";
import type { StorefrontManagers, Versioned } from "./types";
import { commandRecoveryMessage, isVersionConflict, useIdempotentMutation } from "./mutation";

type ManagerCommand =
  | { type: "add"; telegramId: string; etag?: string }
  | { type: "remove"; telegramId: string; etag?: string };

export default function StorefrontManagersPage() {
  const { shop } = useOutletContext<StorefrontOutletContext>();
  const queryClient = useQueryClient();
  const queryKey = storefrontQueryKeys.managers(shop.id);
  const query = useQuery({ queryKey, queryFn: () => listStorefrontManagers(shop.id) });
  const [telegramId, setTelegramId] = useState("");
  const [conflict, setConflict] = useState<ManagerCommand | null>(null);
  const [conflictReloadError, setConflictReloadError] = useState(false);
  const mutation = useIdempotentMutation<Versioned<StorefrontManagers>, ManagerCommand>(
    (input, key) => {
      const etag = input.etag || query.data?.etag || "";
      if (!etag) throw new Error("missing storefront config version");
      return input.type === "add"
        ? addStorefrontManager(shop.id, input.telegramId, etag, key)
        : removeStorefrontManager(shop.id, input.telegramId, etag, key);
    },
    {
      onSuccess: async (result) => {
        if (result.etag) {
          queryClient.setQueryData<Versioned<StorefrontManagers>>(queryKey, (current) =>
            current ? { data: result.data, etag: result.etag } : current);
        }
        setTelegramId("");
        await query.refetch();
      },
      onError: (error, variables) => {
        if (isVersionConflict(error)) setConflict(variables);
      },
    },
  );
  const valid = validTelegramId(telegramId);
  const atLimit = !!query.data
    && query.data.data.items.filter((manager) => manager.role === "manager").length >= query.data.data.max_count;

  return (
    <Box>
      <Stack spacing={0.5} sx={{ mb: 2 }}>
        <Typography variant="h6">مدیران فروشگاه</Typography>
        <Typography variant="body2" color="text.secondary">
          فقط مالک اصلی می‌تواند مدیر تلگرامی اضافه یا حذف کند. این افراد امکان ورود به پورتال ندارند.
        </Typography>
      </Stack>
      {mutation.isError && !isVersionConflict(mutation.error) && (
        <Alert severity="error" sx={{ mb: 2 }}>
          {commandRecoveryMessage(mutation.error) || "تغییر مدیر انجام نشد."}
        </Alert>
      )}
      <DataState isLoading={query.isLoading} isError={query.isError} rows={4} onRetry={() => query.refetch()}>
        {query.data && (
          <Stack spacing={2}>
            <Card>
              <CardContent>
                <Stack direction={{ xs: "column", sm: "row" }} spacing={1.5} alignItems={{ sm: "center" }}>
                  <TextField
                    fullWidth
                    label="Telegram ID مدیر جدید"
                    value={telegramId}
                    error={!!telegramId && !valid}
                    helperText={atLimit ? `حداکثر ${fmtNum(query.data.data.max_count)} مدیر ثبت شده است.` : "شناسهٔ عددی مثبت"}
                    inputProps={{ inputMode: "numeric", dir: "ltr" }}
                    disabled={mutation.isPending}
                    onChange={(event) => setTelegramId(event.target.value.replace(/\D/g, ""))}
                  />
                  <Button
                    variant="contained"
                    startIcon={<PersonAddIcon />}
                    disabled={!valid || atLimit || mutation.isPending}
                    onClick={() => mutation.mutate({ type: "add", telegramId })}
                  >افزودن مدیر</Button>
                </Stack>
              </CardContent>
            </Card>

            <Stack spacing={1}>
              {query.data.data.items.length === 0 ? (
                <Alert severity="info">مدیر اضافه‌ای ثبت نشده است.</Alert>
              ) : query.data.data.items.map((manager) => (
                <Card key={manager.telegram_id}>
                  <CardContent sx={{ "&:last-child": { pb: 2 } }}>
                    <Stack direction="row" alignItems="center" justifyContent="space-between">
                      <Box>
                        <Typography sx={{ fontWeight: 750 }}>{manager.role === "owner" ? "مالک اصلی" : "مدیر تلگرامی"}</Typography>
                        <Typography variant="body2" color="text.secondary" dir="ltr">{String(manager.telegram_id)}</Typography>
                      </Box>
                      <IconButton
                        aria-label={`حذف مدیر ${manager.telegram_id}`}
                        color="error"
                        disabled={!manager.removable || mutation.isPending}
                        onClick={() => window.confirm("این مدیر حذف شود؟") && mutation.mutate({ type: "remove", telegramId: String(manager.telegram_id) })}
                      ><DeleteOutlineIcon /></IconButton>
                    </Stack>
                  </CardContent>
                </Card>
              ))}
            </Stack>
          </Stack>
        )}
      </DataState>

      <StorefrontConflictDialog
        open={!!conflict}
        busy={query.isFetching || mutation.isPending}
        reloadError={conflictReloadError}
        onClose={() => { setConflict(null); setConflictReloadError(false); }}
        onReload={() => void query.refetch().then((fresh) => {
          if (fresh.isSuccess) { setConflict(null); setConflictReloadError(false); setTelegramId(""); }
          else setConflictReloadError(true);
        })}
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

function validTelegramId(value: string) {
  if (!/^\d+$/.test(value)) return false;
  try {
    const parsed = BigInt(value);
    return parsed > 0n && parsed <= 9_223_372_036_854_775_807n;
  } catch {
    return false;
  }
}
