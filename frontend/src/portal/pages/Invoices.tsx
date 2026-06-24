import { useState } from "react";
import { Box, Button, Card, Chip, IconButton, Stack, Table, TableBody, TableCell, TableHead, TableRow, Tooltip, Typography } from "@mui/material";
import PictureAsPdfIcon from "@mui/icons-material/esm/PictureAsPdf";
import PaymentsIcon from "@mui/icons-material/esm/Payments";
import { useQuery } from "@tanstack/react-query";
import { portalInvoices, openPortalInvoicePdf, PortalInvoice } from "../portalClient";
import { DataState } from "../../components/DataState";
import { fmtToman, fmtGb, fmtDate, INVOICE_STATUS_FA } from "../../format";
import { EmptyState } from "../ui";
import PayDialog from "../PayDialog";

const STATUS_COLOR: Record<string, "default" | "info" | "success" | "warning" | "error"> = {
  draft: "default",
  sent: "info",
  paid: "success",
  overdue: "warning",
  enforced: "error",
  canceled: "default",
};

export default function PortalInvoices() {
  const { data = [], isLoading, isError, refetch } = useQuery({
    queryKey: ["portal-invoices"],
    queryFn: portalInvoices,
  });
  const [payInvoice, setPayInvoice] = useState<PortalInvoice | null>(null);
  const [payAll, setPayAll] = useState(false);
  const owedCount = data.filter((inv) => inv.owed).length;

  return (
    <Box>
      <Typography variant="h5" sx={{ mb: 0.4 }}>فاکتورها</Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 2.5 }}>
        فاکتورهای صادرشدهٔ شما — فاکتورهای پرداخت‌نشده با نشانِ «بدهکار»
      </Typography>

      {owedCount > 1 && (
        <Button
          variant="contained"
          color="success"
          startIcon={<PaymentsIcon sx={{ fontSize: 16 }} />}
          onClick={() => setPayAll(true)}
          sx={{ mb: 2 }}
        >
          پرداخت همهٔ بدهی ({owedCount} فاکتور)
        </Button>
      )}

      <DataState isLoading={isLoading} isError={isError} onRetry={refetch} rows={6}>
        {data.length === 0 ? (
          <Card><EmptyState>هنوز فاکتوری برای شما صادر نشده است.</EmptyState></Card>
        ) : (
          <Card sx={{ overflowX: "auto" }}>
            <Table size="small">
              <TableHead>
                <TableRow>
                  <TableCell>شماره</TableCell>
                  <TableCell>دوره</TableCell>
                  <TableCell>پنل</TableCell>
                  <TableCell align="left">حجم</TableCell>
                  <TableCell align="left">مبلغ</TableCell>
                  <TableCell>وضعیت</TableCell>
                  <TableCell>تاریخ</TableCell>
                  <TableCell align="center">عملیات</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {data.map((inv) => (
                  <TableRow key={inv.id} hover>
                    <TableCell dir="ltr" sx={{ fontWeight: 700, fontFamily: "monospace" }}>{inv.number}</TableCell>
                    <TableCell dir="ltr">{inv.period_label}</TableCell>
                    <TableCell>{inv.panel_key}</TableCell>
                    <TableCell align="left">{fmtGb(inv.usage_gb)}</TableCell>
                    <TableCell align="left" sx={{ fontWeight: 750, whiteSpace: "nowrap" }}>{fmtToman(inv.amount_toman)}</TableCell>
                    <TableCell>
                      <Stack direction="row" spacing={0.7} alignItems="center">
                        <Chip
                          size="small"
                          label={INVOICE_STATUS_FA[inv.status] || inv.status}
                          color={STATUS_COLOR[inv.status] || "default"}
                          variant={inv.status === "draft" || inv.status === "canceled" ? "outlined" : "filled"}
                        />
                        {inv.owed && <Chip size="small" color="error" variant="outlined" label="بدهکار" />}
                      </Stack>
                    </TableCell>
                    <TableCell dir="ltr">{fmtDate(inv.created_at)}</TableCell>
                    <TableCell align="center">
                      <Stack direction="row" spacing={0.5} justifyContent="center" alignItems="center">
                        {inv.owed && (
                          <Button
                            size="small"
                            variant="contained"
                            color="success"
                            startIcon={<PaymentsIcon sx={{ fontSize: 16 }} />}
                            onClick={() => setPayInvoice(inv)}
                            sx={{ whiteSpace: "nowrap" }}
                          >
                            پرداخت
                          </Button>
                        )}
                        <Tooltip title="دریافت PDF">
                          <IconButton size="small" onClick={() => openPortalInvoicePdf(inv.id)} aria-label="دانلود PDF فاکتور">
                            <PictureAsPdfIcon fontSize="small" />
                          </IconButton>
                        </Tooltip>
                      </Stack>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </Card>
        )}
      </DataState>

      <PayDialog invoice={payInvoice} onClose={() => setPayInvoice(null)} />
      <PayDialog invoice={null} payAll={payAll} onClose={() => setPayAll(false)} />
    </Box>
  );
}
