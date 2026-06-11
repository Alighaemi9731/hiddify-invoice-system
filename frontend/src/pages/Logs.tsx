import { useState } from "react";
import {
  Box, Card, Chip, Tab, Tabs, Table, TableBody, TableCell, TableHead, TableRow,
} from "@mui/material";
import { useQuery } from "@tanstack/react-query";
import { getDeliveryLog, getEnforcementActions } from "../api/client";
import { DataState } from "../components/DataState";
import { fmtDate } from "../format";

const DELIV_STATUS: any = { sent: ["ارسال‌شده", "success"], failed: ["ناموفق", "error"], blocked: ["مسدود", "error"], unmatched: ["بدون ربات", "warning"] };
const KIND_FA: any = { invoice: "فاکتور", reminder1: "یادآوری ۱", reminder2: "یادآوری ۲", warning: "اخطار", payment_ack: "تأیید پرداخت", generic: "عمومی" };
const ENF_STATUS: any = {
  dry_run: ["آزمایشی", "info"],
  planned: ["در صف", "default"],
  running: ["در حال اجرا", "info"],
  partial: ["نیمه‌کاره", "warning"],
  done: ["انجام‌شده", "success"],
  failed: ["ناموفق", "error"],
  reverted: ["بازگردانده", "warning"],
};
const ACTION_FA: any = { disable_users: "مسدودسازی کاربران و سقف‌ها", restore: "بازگردانی" };

export default function Logs() {
  const [tab, setTab] = useState(0);
  const dq = useQuery({ queryKey: ["delivery-log"], queryFn: () => getDeliveryLog({ limit: 500 }) });
  const aq = useQuery({ queryKey: ["enforcement-actions"], queryFn: getEnforcementActions });
  const deliveries = dq.data ?? [];
  const actions = aq.data ?? [];

  return (
    <Box>
      <Tabs value={tab} onChange={(_, v) => setTab(v)} sx={{ mb: 2 }}>
        <Tab label="گزارش ارسال پیام‌ها" />
        <Tab label="گزارش مسدودسازی" />
      </Tabs>

      {tab === 0 && (
        <DataState isLoading={dq.isLoading} isError={dq.isError} onRetry={dq.refetch}>
        <Card>
          <Table size="small" className="resp-table">
            <TableHead><TableRow>
              <TableCell>نماینده</TableCell><TableCell>نوع</TableCell><TableCell>وضعیت</TableCell>
              <TableCell>خطا</TableCell><TableCell>زمان</TableCell>
            </TableRow></TableHead>
            <TableBody>
              {deliveries.map((d: any) => {
                const [lbl, color] = DELIV_STATUS[d.status] || [d.status, "default"];
                return (
                  <TableRow key={d.id} hover>
                    <TableCell>{d.reseller_name || "—"}</TableCell>
                    <TableCell>{KIND_FA[d.kind] || d.kind}</TableCell>
                    <TableCell><Chip size="small" color={color} label={lbl} /></TableCell>
                    <TableCell sx={{ color: "text.secondary", fontSize: 12 }}>{d.error || ""}</TableCell>
                    <TableCell>{fmtDate(d.created_at)}</TableCell>
                  </TableRow>
                );
              })}
              {deliveries.length === 0 && <TableRow><TableCell colSpan={5} align="center" sx={{ py: 4, color: "text.secondary" }}>گزارشی نیست</TableCell></TableRow>}
            </TableBody>
          </Table>
        </Card>
        </DataState>
      )}

      {tab === 1 && (
        <DataState isLoading={aq.isLoading} isError={aq.isError} onRetry={aq.refetch}>
        <Card>
          <Table size="small" className="resp-table">
            <TableHead><TableRow>
              <TableCell>نماینده</TableCell><TableCell>اقدام</TableCell><TableCell>وضعیت</TableCell>
              <TableCell>تعداد کاربر</TableCell><TableCell>زمان</TableCell>
            </TableRow></TableHead>
            <TableBody>
              {actions.map((a: any) => {
                const [lbl, color] = ENF_STATUS[a.status] || [a.status, "default"];
                return (
                  <TableRow key={a.id} hover>
                    <TableCell>{a.reseller_name || "—"}</TableCell>
                    <TableCell>{ACTION_FA[a.action] || a.action}{a.dry_run ? " (آزمایشی)" : ""}</TableCell>
                    <TableCell><Chip size="small" color={color} label={lbl} /></TableCell>
                    <TableCell>{a.affected_count}</TableCell>
                    <TableCell>{fmtDate(a.created_at)}</TableCell>
                  </TableRow>
                );
              })}
              {actions.length === 0 && <TableRow><TableCell colSpan={5} align="center" sx={{ py: 4, color: "text.secondary" }}>گزارشی نیست</TableCell></TableRow>}
            </TableBody>
          </Table>
        </Card>
        </DataState>
      )}
    </Box>
  );
}
