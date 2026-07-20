import { useEffect, useState } from "react";
import {
  Box, Card, Chip, Table, TableBody, TableCell, TableContainer, TableHead, TableRow,
} from "@mui/material";
import ForumIcon from "@mui/icons-material/esm/Forum";
import BlockIcon from "@mui/icons-material/esm/Block";
import { useQuery } from "@tanstack/react-query";
import { getDeliveryLog, getEnforcementActions } from "../api/client";
import { DataState } from "../components/DataState";
import { TablePager } from "../components/TablePager";
import SegmentedTabs from "../components/SegmentedTabs";
import { fmtDate } from "../format";

const DELIV_STATUS: any = { sent: ["ارسال‌شده", "success"], failed: ["ناموفق", "error"], blocked: ["مسدود", "error"], unmatched: ["بدون ربات", "warning"] };
const KIND_FA: any = { invoice: "فاکتور", reminder1: "یادآوری ۱", reminder2: "یادآوری ۲", warning: "اخطار", payment_ack: "تأیید پرداخت", generic: "عمومی" };
const ENF_STATUS: any = {
  dry_run: ["آزمایشی", "info"],
  planned: ["در صف", "default"],
  running: ["در حال اجرا", "info"],
  partial: ["ناتمام", "warning"],
  done: ["انجام‌شده", "success"],
  failed: ["ناموفق", "error"],
  reverted: ["بازگردانده", "warning"],
};
const ACTION_FA: any = { disable_users: "مسدودسازی کاربران و سقف‌ها", restore: "بازگردانی" };

export default function Logs() {
  const [tab, setTab] = useState(0);
  const [page, setPage] = useState(0);
  const [rpp, setRpp] = useState(50);
  useEffect(() => setPage(0), [tab]);
  const dq = useQuery({ queryKey: ["delivery-log"], queryFn: () => getDeliveryLog({ limit: 500 }) });
  const aq = useQuery({ queryKey: ["enforcement-actions"], queryFn: getEnforcementActions });
  const deliveries = dq.data ?? [];
  const actions = aq.data ?? [];
  const rows = tab === 0 ? deliveries : actions;
  const paged = rows.slice(page * rpp, page * rpp + rpp);
  const pager = (
    <TablePager count={rows.length} page={page} rpp={rpp} onPage={setPage}
      onRpp={(v) => { setRpp(v); setPage(0); }} />
  );

  return (
    <Box>
      <Box sx={{ mb: 2 }}>
        <SegmentedTabs
          value={tab}
          onChange={setTab}
          tabs={[
            { label: "گزارش ارسال پیام‌ها", icon: <ForumIcon fontSize="small" /> },
            { label: "گزارش مسدودسازی", icon: <BlockIcon fontSize="small" /> },
          ]}
        />
      </Box>

      {tab === 0 && (
        <DataState isLoading={dq.isLoading} isError={dq.isError} onRetry={dq.refetch}>
        <Card>
          <TableContainer sx={{ maxHeight: { xs: "none", sm: "calc(100vh - 260px)" } }}>
            <Table size="small" stickyHeader className="resp-table" sx={{ minWidth: { sm: 640 } }}>
              <TableHead><TableRow>
                <TableCell>نماینده</TableCell><TableCell>نوع</TableCell><TableCell>وضعیت</TableCell>
                <TableCell>خطا</TableCell><TableCell>زمان</TableCell>
              </TableRow></TableHead>
              <TableBody>
                {paged.map((d: any) => {
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
                {deliveries.length === 0 && <TableRow><TableCell colSpan={5} align="center" sx={{ py: 4, color: "text.secondary" }}>گزارشی ثبت نشده است</TableCell></TableRow>}
              </TableBody>
            </Table>
          </TableContainer>
          {pager}
        </Card>
        </DataState>
      )}

      {tab === 1 && (
        <DataState isLoading={aq.isLoading} isError={aq.isError} onRetry={aq.refetch}>
        <Card>
          <TableContainer sx={{ maxHeight: { xs: "none", sm: "calc(100vh - 260px)" } }}>
            <Table size="small" stickyHeader className="resp-table" sx={{ minWidth: { sm: 640 } }}>
              <TableHead><TableRow>
                <TableCell>نماینده</TableCell><TableCell>اقدام</TableCell><TableCell>وضعیت</TableCell>
                <TableCell>تعداد کاربر</TableCell><TableCell>زمان</TableCell>
              </TableRow></TableHead>
              <TableBody>
                {paged.map((a: any) => {
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
                {actions.length === 0 && <TableRow><TableCell colSpan={5} align="center" sx={{ py: 4, color: "text.secondary" }}>گزارشی ثبت نشده است</TableCell></TableRow>}
              </TableBody>
            </Table>
          </TableContainer>
          {pager}
        </Card>
        </DataState>
      )}
    </Box>
  );
}
