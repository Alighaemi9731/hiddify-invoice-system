import { useMemo } from "react";
import {
  Box, Button, Chip, Divider, Drawer, IconButton, Stack, Typography,
} from "@mui/material";
import { alpha, useTheme } from "@mui/material/styles";
import CloseIcon from "@mui/icons-material/esm/Close";
import { useQuery } from "@tanstack/react-query";
import { getCrmReseller } from "../../api/client";
import EChart from "../../components/EChart";
import { chartTooltip } from "../../components/chartTooltip";
import { DataState } from "../../components/DataState";
import { fmtDate, fmtDateTime, fmtGb, fmtNum, fmtToman } from "../../format";
import SegmentMessage from "./SegmentMessage";
import { SegmentChip, segmentLabel } from "./segments";

function Fact({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <Box sx={{ minWidth: 120 }}>
      <Typography variant="body2" color="text.secondary">{label}</Typography>
      <Typography sx={{ fontWeight: 600 }}>{value}</Typography>
    </Box>
  );
}

/** One reseller's card: monthly history chart, the numbers behind the segment, and every
 * follow-up ever logged for them. */
export default function ResellerDrawer({
  resellerId, onClose, onFollowup, onClearSnooze,
}: {
  resellerId: number | null;
  onClose: () => void;
  onFollowup: (id: number, name: string, pinnedNote: string) => void;
  onClearSnooze: (id: number) => void;
}) {
  const theme = useTheme();
  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ["crm-reseller", resellerId],
    queryFn: ({ signal }) => getCrmReseller(resellerId as number, signal),
    enabled: resellerId != null,
  });

  const option = useMemo(() => {
    const months = data?.months || [];
    return {
      grid: { left: 8, right: 8, top: 18, bottom: 4, containLabel: true },
      tooltip: {
        trigger: "axis",
        ...chartTooltip(theme),
        valueFormatter: (v: number) => fmtGb(v),
      },
      xAxis: {
        type: "category",
        data: months.map((m) => m.label),
        axisLabel: { fontSize: 11, color: theme.palette.text.secondary },
      },
      yAxis: {
        type: "value",
        axisLabel: { fontSize: 11, color: theme.palette.text.secondary },
      },
      series: [{
        type: "bar",
        data: months.map((m) => m.gb),
        itemStyle: {
          borderRadius: [6, 6, 0, 0],
          color: {
            type: "linear", x: 0, y: 0, x2: 0, y2: 1,
            colorStops: [
              { offset: 0, color: alpha(theme.palette.primary.main, 0.95) },
              { offset: 1, color: alpha(theme.palette.primary.main, 0.45) },
            ],
          },
        },
      }],
    };
  }, [data, theme]);

  const row = data?.row;
  return (
    <Drawer anchor="left" open={resellerId != null} onClose={onClose}
      PaperProps={{ sx: { width: { xs: "100%", sm: 480 }, p: 2.5 } }}>
      <Stack direction="row" alignItems="center" justifyContent="space-between" sx={{ mb: 2 }}>
        <Typography variant="h6" sx={{ fontWeight: 700 }}>{row?.reseller_name || "…"}</Typography>
        <IconButton onClick={onClose} aria-label="بستن"><CloseIcon /></IconButton>
      </Stack>
      <DataState isLoading={isLoading} isError={isError} onRetry={refetch}>
        {row && (
          <Stack spacing={2.5}>
            <Stack direction="row" spacing={1} alignItems="center" useFlexGap flexWrap="wrap">
              <SegmentChip segment={row.segment} />
              <Chip size="small" variant="outlined" label={row.panel_key} />
              {row.sub_resellers > 0 && (
                <Chip size="small" variant="outlined"
                  label={`${fmtNum(row.sub_resellers)} زیرمجموعه`} />
              )}
              {!row.registered && (
                <Chip size="small" variant="outlined" color="warning" label="در ربات ثبت نشده" />
              )}
            </Stack>

            <Stack direction="row" spacing={3} useFlexGap flexWrap="wrap">
              <Fact label="ارزش ماهانه" value={fmtToman(row.value_at_risk_toman)} />
              <Fact label="آخرین فروش"
                value={row.last_sale_date
                  ? `${fmtDate(row.last_sale_date)} (${fmtNum(row.days_since_last_sale || 0)} روز)`
                  : "هرگز"} />
              <Fact label="این ماه"
                value={`${fmtNum(row.mtd_services)} سرویس — ${fmtGb(row.mtd_gb)}`} />
              <Fact label="میانگین ۳ ماه" value={fmtGb(row.avg_prev_gb)} />
              {row.outstanding_toman > 0 && (
                <Fact label="بدهی"
                  value={`${fmtToman(row.outstanding_toman)} (${fmtNum(row.outstanding_count)} فاکتور)`} />
              )}
            </Stack>

            <Box>
              <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
                حجم فروش ماهانه (گیگابایت)
              </Typography>
              <EChart option={option} height={200}
                ariaLabel={`نمودار حجم فروش ماهانهٔ ${row.reseller_name}`} />
            </Box>

            {row.note && (
              <Box>
                <Typography variant="body2" color="text.secondary">یادداشت ثابت</Typography>
                <Typography sx={{ whiteSpace: "pre-wrap" }}>{row.note}</Typography>
              </Box>
            )}

            {/* Here the greeting carries the reseller's own name — one chat, one reader. */}
            <SegmentMessage segment={row.segment} name={row.reseller_name} dense />

            <Stack direction="row" spacing={1}>
              <Button variant="contained" size="small"
                onClick={() => onFollowup(row.reseller_id, row.reseller_name, row.note)}>
                ثبت پیگیری
              </Button>
              {!row.due && (
                <Button variant="outlined" size="small"
                  onClick={() => onClearSnooze(row.reseller_id)}>
                  برگرداندن به فهرست
                </Button>
              )}
            </Stack>

            <Divider />
            <Box>
              <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
                تاریخچهٔ پیگیری ({fmtNum(data.followups.length)})
              </Typography>
              {data.followups.length === 0 && (
                <Typography variant="body2" color="text.secondary">
                  هنوز هیچ پیگیری‌ای برای این نماینده ثبت نشده است.
                </Typography>
              )}
              <Stack spacing={1.5}>
                {data.followups.map((f) => (
                  <Box key={f.id} sx={{ borderInlineStart: 2, borderColor: "divider", pl: 1.5 }}>
                    <Stack direction="row" spacing={1} alignItems="center">
                      <Typography variant="body2" color="text.secondary">
                        {fmtDateTime(f.created_at)}
                      </Typography>
                      <Chip size="small" variant="outlined" label={segmentLabel(f.segment)} />
                      {f.muted && <Chip size="small" variant="outlined" label="بی‌خیال" />}
                    </Stack>
                    {f.note && (
                      <Typography sx={{ whiteSpace: "pre-wrap" }}>{f.note}</Typography>
                    )}
                  </Box>
                ))}
              </Stack>
            </Box>
          </Stack>
        )}
      </DataState>
    </Drawer>
  );
}
