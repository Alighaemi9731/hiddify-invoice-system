import { useState } from "react";
import {
  Alert, Box, Button, Chip, CircularProgress, Dialog, DialogContent, DialogTitle,
  Divider, IconButton, Stack, TextField, ToggleButton, ToggleButtonGroup, Tooltip, Typography,
} from "@mui/material";
import ContentCopyIcon from "@mui/icons-material/esm/ContentCopy";
import UploadFileIcon from "@mui/icons-material/esm/UploadFile";
import { QRCodeSVG } from "qrcode.react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  portalPayOptions, portalPayOptionsAll, portalPayTxid, portalPayScreenshot, PortalInvoice,
} from "./portalClient";
import { useToast, errMsg } from "../components/Toast";
import { fmtToman } from "../format";
import { useXsFullScreen } from "../responsive";

function QrBox({ value }: { value: string }) {
  return (
    <Box sx={{ mt: 1, display: "flex", justifyContent: "center" }}>
      <Box sx={{ p: 1, bgcolor: "#fff", borderRadius: 2, lineHeight: 0 }}>
        <QRCodeSVG value={value} size={132} level="M" />
      </Box>
    </Box>
  );
}

function CopyRow({ label, value, show }: { label: string; value: string; show: (m: string, s?: any) => void }) {
  return (
    <Box sx={{ mt: 0.8 }}>
      <Typography variant="caption" color="text.secondary">{label}</Typography>
      <Box
        dir="ltr"
        tabIndex={0}
        role="group"
        sx={{
          mt: 0.3, p: 1, borderRadius: 2, bgcolor: "action.hover",
          fontFamily: "monospace", fontSize: 13, wordBreak: "break-all",
          display: "flex", alignItems: "center", gap: 1,
        }}
      >
        <Box sx={{ flex: 1, minWidth: 0 }}>{value}</Box>
        <Tooltip title="کپی">
          <IconButton
            size="small"
            onClick={async () => {
              try { await navigator.clipboard.writeText(value); show("کپی شد", "success"); }
              catch { show("کپی نشد", "error"); }
            }}
          >
            <ContentCopyIcon sx={{ fontSize: 16 }} />
          </IconButton>
        </Tooltip>
      </Box>
    </Box>
  );
}

export default function PayDialog({
  invoice, payAll, onClose,
}: { invoice: PortalInvoice | null; payAll?: boolean; onClose: () => void }) {
  const open = !!invoice || !!payAll;
  const xsFull = useXsFullScreen();
  const qc = useQueryClient();
  const { node: toast, show } = useToast();
  const [chain, setChain] = useState<"bsc" | "ton" | "avax">("bsc");
  const [txid, setTxid] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);

  // Single-invoice options OR all-payable options (one transfer settles them all).
  const { data: raw, isLoading, isError } = useQuery({
    queryKey: payAll ? ["portal-pay-options-all"] : ["portal-pay-options", invoice?.id],
    queryFn: (): Promise<any> => (payAll ? portalPayOptionsAll() : portalPayOptions(invoice!.id)),
    enabled: open,
  });
  // Normalize both shapes into one: amounts, methods, the target invoice ids, and gating.
  const opts = raw
    ? payAll
      ? {
          amount_toman: (raw as any).total_amount_toman,
          amount_usdt: (raw as any).total_amount_usdt,
          methods: (raw as any).methods,
          invoice_ids: (raw as any).invoice_ids as number[],
          count: (raw as any).count as number,
          pending: false,
          payable: (raw as any).count > 0,
        }
      : {
          amount_toman: (raw as any).invoice.amount_toman,
          amount_usdt: (raw as any).invoice.amount_usdt,
          methods: (raw as any).methods,
          invoice_ids: [(raw as any).invoice.id] as number[],
          count: 1,
          pending: (raw as any).pending as boolean,
          payable: (raw as any).payable as boolean,
        }
    : null;

  const m = opts?.methods;
  const canTxid = !!(m?.usdt || m?.ton || m?.avax);
  const canReceipt = !!(m?.card || m?.screenshot);
  // Default the chain selector to whichever crypto method is enabled.
  const chainOptions: ("bsc" | "ton" | "avax")[] = [
    ...(m?.usdt ? ["bsc" as const] : []),
    ...(m?.ton ? ["ton" as const] : []),
    ...(m?.avax ? ["avax" as const] : []),
  ];
  const effChain = chainOptions.includes(chain) ? chain : chainOptions[0] || "bsc";

  const finish = (msg: string) => {
    show(msg, "success");
    qc.invalidateQueries({ queryKey: ["portal-invoices"] });
    qc.invalidateQueries({ queryKey: ["portal-payments"] });
    qc.invalidateQueries({ queryKey: ["portal-pay-options"] });
    qc.invalidateQueries({ queryKey: ["portal-pay-options-all"] });
    setTxid(""); setFile(null);
    setTimeout(onClose, 800);
  };

  const submitTxid = async () => {
    if (!opts || !txid.trim()) { show("شناسهٔ تراکنش را وارد کنید", "warning"); return; }
    setBusy(true);
    try {
      const r = await portalPayTxid({ invoice_ids: opts.invoice_ids, txid: txid.trim(), chain: effChain });
      // `message` was written before the on-chain check ran; when the check settled the payment
      // outright, say so instead of "waiting for review".
      if (r.status === "ok" || r.status === "reopened") {
        finish(r.auto_confirmed
          ? "✅ واریزی شما روی زنجیره بررسی و تأیید شد؛ فاکتور تسویه شد."
          : r.message);
      } else show(r.message, "warning");
    } catch (e) { show(errMsg(e), "error"); }
    finally { setBusy(false); }
  };

  const submitReceipt = async () => {
    if (!opts || !file) { show("یک تصویر رسید انتخاب کنید", "warning"); return; }
    setBusy(true);
    try {
      const r = await portalPayScreenshot(opts.invoice_ids, file);
      if (r.status === "ok") finish(r.message);
      else show(r.message, "warning");
    } catch (e) { show(errMsg(e), "error"); }
    finally { setBusy(false); }
  };

  return (
    <Dialog open={open} onClose={busy ? undefined : onClose} maxWidth="sm" fullWidth fullScreen={xsFull}>
      {toast}
      <DialogTitle sx={{ fontWeight: 800 }}>
        {payAll
          ? `پرداخت همهٔ بدهی${opts && opts.count > 1 ? ` (${opts.count} فاکتور)` : ""}`
          : `پرداخت فاکتور ${invoice?.period_label ?? ""}`}
      </DialogTitle>
      <DialogContent dividers>
        {isLoading ? (
          <Box sx={{ display: "grid", placeItems: "center", py: 4 }}><CircularProgress /></Box>
        ) : isError || !opts ? (
          <Alert severity="error">خطا در بارگذاری اطلاعات پرداخت.</Alert>
        ) : opts.pending ? (
          <Alert severity="info">برای این فاکتور قبلاً پرداختی ثبت شده و در انتظار تأیید است.</Alert>
        ) : !opts.payable ? (
          <Alert severity="warning">
            {payAll ? "بدهی قابل پرداختی ندارید." : "این فاکتور در حال حاضر قابل پرداخت نیست."}
          </Alert>
        ) : (
          <Stack spacing={1.5}>
            <Stack direction="row" alignItems="center" justifyContent="space-between">
              <Typography color="text.secondary">
                {payAll ? `مبلغ کل (${opts.count} فاکتور):` : "مبلغ قابل پرداخت:"}
              </Typography>
              <Typography sx={{ fontWeight: 850, fontSize: 18 }}>{fmtToman(opts.amount_toman)}</Typography>
            </Stack>
            <Divider />

            {m!.usdt && (
              <Box>
                <Chip size="small" color="success" label="USDT — شبکهٔ BEP-20 (BSC)" sx={{ fontWeight: 700 }} />
                <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 0.6 }}>
                  مبلغ: {opts.amount_usdt.toLocaleString("en-US", { maximumFractionDigits: 2 })} USDT
                  — ⚠️ فقط شبکهٔ BEP-20؛ واریز از شبکه‌ای دیگر موجبِ از دست رفتنِ وجه می‌شود.
                </Typography>
                <CopyRow label="آدرس کیف پول USDT" value={m!.wallet} show={show} />
                <QrBox value={m!.wallet} />
              </Box>
            )}
            {m!.ton && (
              <Box>
                <Chip size="small" color="info" label="گرام (GRAM)" sx={{ fontWeight: 700 }} />
                {m!.amount_ton != null && (
                  <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 0.6 }}>
                    مبلغ تقریبی: {m!.amount_ton.toLocaleString("en-US", { maximumFractionDigits: 2 })} GRAM — فقط روی شبکهٔ TON واریز شود.
                  </Typography>
                )}
                <CopyRow label="آدرس کیف پول گرام (شبکهٔ TON)" value={m!.ton_address} show={show} />
                <QrBox value={m!.ton_address} />
              </Box>
            )}
            {m!.avax && (
              <Box>
                <Chip size="small" color="error" label="اوالانچ (AVAX)" sx={{ fontWeight: 700 }} />
                {m!.amount_avax != null && (
                  <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 0.6 }}>
                    مبلغ تقریبی: {m!.amount_avax.toLocaleString("en-US", { maximumFractionDigits: 4 })} AVAX — فقط روی شبکهٔ Avalanche C-Chain واریز شود.
                  </Typography>
                )}
                <CopyRow label="آدرس کیف پول اوالانچ (AVAX)" value={m!.avax_address} show={show} />
                <QrBox value={m!.avax_address} />
              </Box>
            )}
            {m!.card && (
              <Box>
                <Chip size="small" color="warning" label="کارت‌به‌کارت" sx={{ fontWeight: 700 }} />
                <CopyRow label="شمارهٔ کارت" value={m!.card_number} show={show} />
                {m!.card_holder && (
                  <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 0.6 }}>
                    به نام: {m!.card_holder}
                  </Typography>
                )}
              </Box>
            )}

            <Divider />

            {canTxid && (
              <Box>
                <Typography variant="subtitle2" sx={{ fontWeight: 700, mb: 1 }}>
                  ارسال شناسهٔ تراکنش (TXID)
                </Typography>
                {chainOptions.length > 1 && (
                  <ToggleButtonGroup
                    size="small"
                    exclusive
                    value={effChain}
                    onChange={(_, v) => v && setChain(v)}
                    sx={{ mb: 1 }}
                  >
                    {m!.usdt && <ToggleButton value="bsc">USDT</ToggleButton>}
                    {m!.ton && <ToggleButton value="ton">GRAM</ToggleButton>}
                    {m!.avax && <ToggleButton value="avax">AVAX</ToggleButton>}
                  </ToggleButtonGroup>
                )}
                <TextField
                  fullWidth
                  size="small"
                  placeholder="شناسهٔ تراکنش یا لینک آن را اینجا بچسبانید"
                  value={txid}
                  onChange={(e) => setTxid(e.target.value)}
                  inputProps={{ dir: "ltr" }}
                />
                <Button
                  variant="contained"
                  sx={{ mt: 1 }}
                  disabled={busy || !txid.trim()}
                  onClick={submitTxid}
                >
                  ارسال شناسهٔ تراکنش
                </Button>
              </Box>
            )}

            {canReceipt && (
              <Box>
                <Typography variant="subtitle2" sx={{ fontWeight: 700, mb: 1, mt: canTxid ? 1 : 0 }}>
                  ارسال تصویر رسید
                </Typography>
                <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap">
                  <Button component="label" variant="outlined" startIcon={<UploadFileIcon />}>
                    انتخاب تصویر
                    <input
                      type="file"
                      accept="image/*"
                      hidden
                      onChange={(e) => setFile(e.target.files?.[0] || null)}
                    />
                  </Button>
                  {file && <Typography variant="caption" noWrap sx={{ maxWidth: 160 }}>{file.name}</Typography>}
                  <Button variant="contained" disabled={busy || !file} onClick={submitReceipt}>
                    ارسال رسید
                  </Button>
                </Stack>
              </Box>
            )}

            {!canTxid && !canReceipt && (
              <Alert severity="info">روشِ پرداختی پیکربندی نشده است؛ با پشتیبانی هماهنگ کنید.</Alert>
            )}
          </Stack>
        )}
      </DialogContent>
    </Dialog>
  );
}
