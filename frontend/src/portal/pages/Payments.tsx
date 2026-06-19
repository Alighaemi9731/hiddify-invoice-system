import { Box, Card, Chip, Link, Table, TableBody, TableCell, TableHead, TableRow, Typography } from "@mui/material";
import { useQuery } from "@tanstack/react-query";
import { portalPayments } from "../portalClient";
import { DataState } from "../../components/DataState";
import { fmtToman, fmtDate, PAYMENT_STATUS_FA, PAYMENT_METHOD_FA } from "../../format";
import { EmptyState } from "../ui";

const STATUS_COLOR: Record<string, "default" | "warning" | "success" | "error"> = {
  pending: "warning",
  confirmed: "success",
  rejected: "error",
};

// A clickable explorer link for the tx hash (tonscan for TON, bscscan for BSC/USDT).
function explorerHref(chain: string | null, txid: string | null): string | null {
  if (!txid) return null;
  if (chain === "ton") return `https://tonscan.org/tx/${txid}`;
  return `https://bscscan.com/tx/${txid}`;
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
        تاریخچهٔ پرداخت‌های شما و وضعیتِ تأییدِ هرکدام
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
                  <TableCell>وضعیت</TableCell>
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
                      <TableCell dir="ltr">{p.invoice_period || "—"}</TableCell>
                      <TableCell align="left" sx={{ whiteSpace: "nowrap" }}>
                        {p.amount_toman ? fmtToman(p.amount_toman) : "—"}
                      </TableCell>
                      <TableCell dir="ltr" sx={{ maxWidth: 180, overflow: "hidden", textOverflow: "ellipsis" }}>
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
                        <Chip size="small" label={PAYMENT_STATUS_FA[p.status] || p.status} color={STATUS_COLOR[p.status] || "default"} />
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
