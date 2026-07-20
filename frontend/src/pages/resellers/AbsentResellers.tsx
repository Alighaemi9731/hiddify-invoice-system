import { useEffect, useState } from "react";
import {
  Button,
  Card,
  Chip,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  IconButton,
  Skeleton,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TablePagination,
  TableRow,
  Tooltip,
  Typography,
} from "@mui/material";
import DeleteOutlineIcon from "@mui/icons-material/esm/DeleteOutline";
import { useQuery } from "@tanstack/react-query";
import { AbsentReseller, deleteAbsentReseller, listAbsentResellers } from "../../api/client";
import { SortTh, useSort } from "../../components/sortable";
import { useToast } from "../../components/Toast";
import { useDialogState } from "../../hooks/useDialogState";
import { useToastMutation } from "../../hooks/useToastMutation";
import { fmtDate, fmtNum } from "../../format";

// ── Absent resellers (removed from the panel, row still lingering) ────────────
export default function AbsentResellers({ panelId }: { panelId: string }) {
  const { node: toastNode, show } = useToast();
  const confirmDlg = useDialogState<AbsentReseller>();
  const [page, setPage] = useState(0);

  const { data = [], isLoading } = useQuery({
    queryKey: ["absent-resellers", panelId],
    queryFn: () => listAbsentResellers({ panel_id: panelId ? Number(panelId) : undefined }),
  });
  useEffect(() => { setPage(0); }, [panelId]);
  const { sorted, key, dir, toggle } = useSort(data, "last_seen_at", "asc");
  const rows = sorted.slice(page * 25, page * 25 + 25);

  const del = useToastMutation({
    show,
    mutationFn: (id: number) => deleteAbsentReseller(id),
    success: (r: any) =>
      `حذف شد: ${fmtNum(r?.resellers_deleted ?? 1)} نماینده و ${fmtNum(r?.users_deleted ?? 0)} کاربر (تاریخچهٔ مالی حفظ شد).`,
    onSuccess: () => confirmDlg.close(),
    invalidate: ["absent-resellers", "resellers", "reseller-tree"],
  });
  const confirmRow = confirmDlg.data;

  return (
    <Card sx={{ overflow: "hidden" }}>
      <Typography variant="body2" color="text.secondary" sx={{ p: 2, pb: 0 }}>
        {fmtNum(data.length)} نمایندهٔ حذف‌شده از پنل (ردیف آن‌ها هنوز در سامانه باقی مانده است).
      </Typography>
      {isLoading ? (
        <Stack spacing={1} sx={{ p: 2 }}>
          {[0, 1, 2].map((i) => <Skeleton key={i} variant="rounded" height={56} />)}
        </Stack>
      ) : (
        <>
          <Table size="small" className="resp-table">
            <TableHead>
              <TableRow>
                <SortTh id="name" label="نماینده" sortKey={key} dir={dir} onSort={toggle} />
                <SortTh id="panel_key" label="پنل" sortKey={key} dir={dir} onSort={toggle} />
                <SortTh id="last_seen_at" label="آخرین حضور" sortKey={key} dir={dir} onSort={toggle} />
                <SortTh id="users_count" label="کاربر" sortKey={key} dir={dir} onSort={toggle} />
                <SortTh id="sub_resellers" label="زیرمجموعه" sortKey={key} dir={dir} onSort={toggle} />
                <TableCell align="left">عملیات</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {rows.map((r) => (
                <TableRow key={r.id} hover>
                  <TableCell data-label="نماینده">{r.name}</TableCell>
                  <TableCell data-label="پنل"><Chip size="small" label={r.panel_key} variant="outlined" /></TableCell>
                  <TableCell data-label="آخرین حضور" dir="ltr">{r.last_seen_at ? fmtDate(r.last_seen_at) : "—"}</TableCell>
                  <TableCell data-label="کاربر">{fmtNum(r.users_count)}</TableCell>
                  <TableCell data-label="زیرمجموعه">
                    {r.sub_resellers > 0
                      ? <Chip size="small" color="warning" variant="outlined" label={fmtNum(r.sub_resellers)} />
                      : fmtNum(0)}
                  </TableCell>
                  <TableCell data-label="عملیات" align="left">
                    <Tooltip title="حذف ردیف این نمایندهٔ غایب">
                      <span><IconButton size="small" color="error" disabled={del.isPending}
                        onClick={() => confirmDlg.openWith(r)}><DeleteOutlineIcon fontSize="small" /></IconButton></span>
                    </Tooltip>
                  </TableCell>
                </TableRow>
              ))}
              {data.length === 0 && (
                <TableRow>
                  <TableCell colSpan={6} align="center" sx={{ py: 7, color: "text.secondary" }}>
                    نماینده‌ای که از پنل حذف شده باشد وجود ندارد.
                  </TableCell>
                </TableRow>
              )}
            </TableBody>
          </Table>
          {data.length > 25 && (
            <TablePagination
              component="div" count={data.length} page={page}
              rowsPerPage={25} rowsPerPageOptions={[25]}
              onPageChange={(_e, p) => setPage(p)}
              labelDisplayedRows={({ from, to, count }) => `${fmtNum(from)}–${fmtNum(to)} از ${fmtNum(count)}`}
            />
          )}
        </>
      )}

      <Dialog open={confirmDlg.open} onClose={confirmDlg.close} fullWidth maxWidth="xs">
        {confirmRow && (<>
          <DialogTitle>حذف نمایندهٔ غایب — {confirmRow.name}</DialogTitle>
          <DialogContent>
            <Typography variant="body2" sx={{ mb: 1 }}>
              این نماینده، زیرمجموعه‌های <b>غایب</b> زیر آن، و <b>کاربران</b> همهٔ آن‌ها برای همیشه حذف می‌شوند
              (به‌همراه فاکتورها و پرداخت‌های آن‌ها). <b>تاریخچهٔ مالی (لجر) حفظ می‌شود.</b>
            </Typography>
            {confirmRow.has_nondraft_invoices && (
              <Typography variant="body2" color="error" sx={{ fontWeight: 700, mb: 1 }}>
                ⚠️ این نماینده فاکتور ارسال‌شده یا پرداخت‌شده دارد؛ آن فاکتورها نیز حذف خواهند شد.
              </Typography>
            )}
            {confirmRow.has_payments && (
              <Typography variant="body2" color="error" sx={{ fontWeight: 700, mb: 1 }}>
                ⚠️ پرداخت‌های ثبت‌شدهٔ این نماینده نیز حذف می‌شوند.
              </Typography>
            )}
            {confirmRow.sub_resellers > 0 && (
              <Typography variant="body2" color="warning.main" sx={{ fontWeight: 700 }}>
                ⚠️ این نماینده {fmtNum(confirmRow.sub_resellers)} زیرمجموعه دارد؛ زیرمجموعه‌های غایب حذف می‌شوند، اما هر زیرمجموعه‌ای که هنوز روی پنل حاضر است دست‌نخورده باقی می‌ماند.
              </Typography>
            )}
          </DialogContent>
          <DialogActions>
            <Button onClick={confirmDlg.close}>انصراف</Button>
            <Button variant="contained" color="error" disabled={del.isPending}
              onClick={() => del.mutate(confirmRow.id)}>حذف</Button>
          </DialogActions>
        </>)}
      </Dialog>
      {toastNode}
    </Card>
  );
}
