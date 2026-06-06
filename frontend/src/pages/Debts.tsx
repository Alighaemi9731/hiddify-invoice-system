import { Box, Card, Chip, Table, TableBody, TableCell, TableHead, TableRow, Typography } from "@mui/material";
import { useQuery } from "@tanstack/react-query";
import { getDebts } from "../api/client";
import { useSort, SortTh } from "../components/sortable";
import { fmtToman, fmtNum } from "../format";

export default function Debts() {
  const { data = [] } = useQuery({ queryKey: ["debts"], queryFn: getDebts });
  const { sorted, key, dir, toggle } = useSort(data, "outstanding_toman", "desc");
  const total = data.reduce((s: number, d: any) => s + d.outstanding_toman, 0);

  return (
    <Box>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
        مجموع بدهی معوق: <b>{fmtToman(total)}</b> — {fmtNum(data.length)} نماینده
      </Typography>
      <Card>
        <Table size="small" className="resp-table">
          <TableHead>
            <TableRow>
              <SortTh id="reseller_name" label="نماینده" sortKey={key} dir={dir} onSort={toggle} />
              <SortTh id="panel_key" label="پنل" sortKey={key} dir={dir} onSort={toggle} />
              <SortTh id="bot_registered" label="ربات" sortKey={key} dir={dir} onSort={toggle} />
              <SortTh id="invoices_count" label="تعداد فاکتور" sortKey={key} dir={dir} onSort={toggle} />
              <SortTh id="outstanding_toman" label="بدهی (تومان)" sortKey={key} dir={dir} onSort={toggle} />
              <SortTh id="oldest_period" label="قدیمی‌ترین دوره" sortKey={key} dir={dir} onSort={toggle} />
            </TableRow>
          </TableHead>
          <TableBody>
            {sorted.map((d: any) => (
              <TableRow key={d.reseller_id} hover>
                <TableCell>{d.reseller_name}</TableCell>
                <TableCell>{d.panel_key}</TableCell>
                <TableCell>{d.bot_registered ? <Chip size="small" color="success" label="متصل" /> : <Chip size="small" color="error" label="بدون ربات" />}</TableCell>
                <TableCell>{fmtNum(d.invoices_count)}</TableCell>
                <TableCell>{fmtToman(d.outstanding_toman)}</TableCell>
                <TableCell dir="ltr">{d.oldest_period}</TableCell>
              </TableRow>
            ))}
            {data.length === 0 && <TableRow><TableCell colSpan={7} align="center" sx={{ py: 4, color: "text.secondary" }}>بدهی معوقی وجود ندارد</TableCell></TableRow>}
          </TableBody>
        </Table>
      </Card>
    </Box>
  );
}
