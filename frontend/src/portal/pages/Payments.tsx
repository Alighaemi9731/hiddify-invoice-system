import { Box, Card, Chip, IconButton, Link, Stack, Table, TableBody, TableCell, TableHead, TableRow, Tooltip, Typography } from "@mui/material";
import { alpha, useTheme } from "@mui/material/styles";
import ImageIcon from "@mui/icons-material/esm/Image";
import { useQuery } from "@tanstack/react-query";
import { portalPayments, openPortalPaymentProof, PortalPayment } from "../portalClient";
import { DataState } from "../../components/DataState";
import { fmtToman, fmtDate, PAYMENT_STATUS_FA, PAYMENT_METHOD_FA } from "../../format";
import { EmptyState } from "../ui";

const STATUS_COLOR: Record<string, "default" | "warning" | "success" | "error"> = {
  pending: "warning",
  confirmed: "success",
  rejected: "error",
};

// A clickable explorer link for the tx hash (tonscan for TON, snowtrace for AVAX, bscscan for BSC/USDT).
function explorerHref(chain: string | null, txid: string | null): string | null {
  if (!txid) return null;
  if (chain === "ton") return `https://tonscan.org/tx/${txid}`;
  if (chain === "avax") return `https://snowtrace.io/tx/${txid}`;
  return `https://bscscan.com/tx/${txid}`;
}

// Compact 3-step timeline: ثبت → بررسی → نتیجه. Confirmed = all green; rejected = last red.
function StatusSteps({ p }: { p: PortalPayment }) {
  const theme = useTheme();
  const ok = theme.palette.success.main;
  const bad = theme.palette.error.main;
  const wait = theme.palette.warning.main;
  const idle = alpha(theme.palette.text.secondary, 0.3);
  let colors: string[];
  let label: string;
  if (p.status === "confirmed") { colors = [ok, ok, ok]; label = "تأیید شد"; }
  else if (p.status === "rejected") { colors = [ok, ok, bad]; label = "رد شد"; }
  else { colors = [ok, wait, idle]; label = "در انتظارِ بررسی"; }
  return (
    <Tooltip title={label}>
      <Stack direction="row" spacing={0.5} alignItems="center">
        {colors.map((c, i) => (
          <Box key={i} sx={{ width: 9, height: 9, borderRadius: "50%", bgcolor: c }} />
        ))}
        <Typography variant="caption" sx={{ ml: 0.5 }}>{label}</Typography>
      </Stack>
    </Tooltip>
  );
}

export default function PortalPayments() {
  const { data = [], isLoading, isError, refetch } = useQuery({
    queryKey: ["portal-payments"],
    queryFn: portalPayments,
  });

  return (
    <Box>
      <Typography variant="h5" sx={{ mb: 0.4 }}>پرداخت‌ها</Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 2.5 }}>
        تاریخچهٔ پرداخت‌های شما، روندِ بررسی و رسیدِ هرکدام
      </Typography>

      <DataState isLoading={isLoading} isError={isError} onRetry={refetch} rows={6}>
        {data.length === 0 ? (
          <Card><EmptyState>هنوز پرداختی ثبت نکرده‌اید.</EmptyState></Card>
        ) : (
          <Card sx={{ overflowX: "auto" }}>
            <Table size="small">
              <TableHead>
                <TableRow>
                  <TableCell>پیگیری</TableCell>
                  <TableCell>روش</TableCell>
                  <TableCell>فاکتور (دوره)</TableCell>
                  <TableCell align="left">مبلغ</TableCell>
                  <TableCell>شناسهٔ تراکنش</TableCell>
                  <TableCell>روندِ بررسی</TableCell>
                  <TableCell align="center">رسید</TableCell>
                  <TableCell>تاریخ</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {data.map((p) => {
                  const href = explorerHref(p.chain, p.txid);
                  return (
                    <TableRow key={p.number} hover>
                      <TableCell dir="ltr" sx={{ fontWeight: 700, fontFamily: "monospace" }}>{p.number}</TableCell>
                      <TableCell>{PAYMENT_METHOD_FA[p.method] || p.method}</TableCell>
                      <TableCell dir="ltr">
                        {p.invoice_count > 1 ? `${p.invoice_count} فاکتور: ${p.invoice_period}` : (p.invoice_period || "—")}
                      </TableCell>
                      <TableCell align="left" sx={{ whiteSpace: "nowrap" }}>
                        {p.amount_toman ? fmtToman(p.amount_toman) : "—"}
                      </TableCell>
                      <TableCell dir="ltr" sx={{ maxWidth: 160, overflow: "hidden", textOverflow: "ellipsis" }}>
                        {p.txid ? (
                          href ? (
                            <Link href={href} target="_blank" rel="noopener noreferrer" sx={{ fontFamily: "monospace", fontSize: 12 }}>
                              {p.txid.slice(0, 10)}…
                            </Link>
                          ) : (
                            <span style={{ fontFamily: "monospace", fontSize: 12 }}>{p.txid.slice(0, 10)}…</span>
                          )
                        ) : "—"}
                      </TableCell>
                      <TableCell>
                        <Chip size="small" sx={{ display: { xs: "inline-flex", md: "none" } }}
                          label={PAYMENT_STATUS_FA[p.status] || p.status} color={STATUS_COLOR[p.status] || "default"} />
                        <Box sx={{ display: { xs: "none", md: "block" } }}><StatusSteps p={p} /></Box>
                      </TableCell>
                      <TableCell align="center">
                        {p.has_proof ? (
                          <Tooltip title="مشاهدهٔ رسید">
                            <IconButton size="small" onClick={() => openPortalPaymentProof(p.id)} aria-label="مشاهدهٔ رسید">
                              <ImageIcon fontSize="small" />
                            </IconButton>
                          </Tooltip>
                        ) : "—"}
                      </TableCell>
                      <TableCell dir="ltr">{fmtDate(p.created_at)}</TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          </Card>
        )}
      </DataState>
    </Box>
  );
}
