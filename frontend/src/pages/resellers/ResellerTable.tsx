import { ReactNode } from "react";
import {
  Chip,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Typography,
} from "@mui/material";
import { ResellerRow, ResellerTreeRow } from "../../api/client";
import CapacityBar from "../../components/CapacityBar";
import TelegramLink from "../../components/TelegramLink";
import { Dir, SortTh } from "../../components/sortable";
import { fmtNum } from "../../format";
import { ConnectionStatus, EnforcementStatus, ResellerIdentity } from "./ResellerIdentity";
import { VisibleTreeRow } from "./useResellerTree";

function ResellerTableRow({
  reseller,
  depth = 0,
  tree = false,
  expanded,
  onToggle,
  actions,
  canAddSwitch,
}: {
  reseller: ResellerRow | ResellerTreeRow;
  depth?: number;
  tree?: boolean;
  expanded?: boolean;
  onToggle?: () => void;
  actions: (reseller: ResellerRow) => ReactNode;
  canAddSwitch: (reseller: ResellerRow) => ReactNode;
}) {
  return (
    <TableRow
      hover
      // Tree rows render EXACTLY like the main-list rows — no background tint of any kind (a tint
      // over the translucent glass surface looked foggy/matte). Hierarchy is shown only by the
      // indentation, connectors, chevrons and bold root names.
      sx={{ "& td": { py: 1.05 } }}
    >
      <TableCell sx={{ minWidth: 260 }}>
        <ResellerIdentity
          reseller={reseller}
          depth={depth}
          tree={tree}
          expanded={expanded}
          onToggle={onToggle}
        />
      </TableCell>
      <TableCell align="center"><TelegramLink username={reseller.username} chatId={reseller.bot_chat_id} /></TableCell>
      <TableCell>
        <Chip size="small" label={reseller.panel_key} variant="outlined" />
      </TableCell>
      <TableCell>
        <Typography variant="body2" sx={{ fontWeight: 750, whiteSpace: "nowrap" }}>
          {fmtNum(reseller.effective_price_per_gb)}
          <Typography component="span" variant="caption" color="text.secondary"> تومان</Typography>
        </Typography>
        {!reseller.price_per_gb && (
          <Typography variant="caption" color="text.secondary">پیش‌فرض</Typography>
        )}
      </TableCell>
      <TableCell>
        <CapacityBar used={reseller.users_count} max={reseller.panel_max_users} />
      </TableCell>
      <TableCell>{canAddSwitch(reseller)}</TableCell>
      <TableCell><ConnectionStatus connected={reseller.registered} /></TableCell>
      <TableCell><EnforcementStatus state={reseller.enforcement_state} /></TableCell>
      <TableCell>
        {reseller.exclude_from_billing
          ? <Chip size="small" color="warning" variant="outlined" label="معاف" />
          : <Chip size="small" color="success" variant="outlined" label="محاسبه می‌شود" />}
      </TableCell>
      <TableCell align="left">{actions(reseller)}</TableCell>
    </TableRow>
  );
}

export default function ResellerTable({
  tab,
  rows,
  visibleTreeRows,
  expanded,
  onToggleBranch,
  sortKey,
  dir,
  onSort,
  currentCount,
  actions,
  canAddSwitch,
}: {
  tab: number;
  rows: ResellerRow[];
  visibleTreeRows: VisibleTreeRow[];
  expanded: Set<number>;
  onToggleBranch: (id: number) => void;
  sortKey: string;
  dir: Dir;
  onSort: (column: string) => void;
  currentCount: number;
  actions: (reseller: ResellerRow) => ReactNode;
  canAddSwitch: (reseller: ResellerRow) => ReactNode;
}) {
  return (
    <TableContainer>
      <Table size="small" sx={{ minWidth: 1080 }}>
        <TableHead>
          <TableRow>
            <SortTh id="name" label="نماینده" sortKey={sortKey} dir={dir} onSort={onSort} />
            <TableCell align="center">تلگرام</TableCell>
            <SortTh id="panel_key" label="پنل" sortKey={sortKey} dir={dir} onSort={onSort} />
            <SortTh id="effective_price_per_gb" label="قیمت/گیگ" sortKey={sortKey} dir={dir} onSort={onSort} />
            <SortTh id="capacity_pct" label="پُری ظرفیت" sortKey={sortKey} dir={dir} onSort={onSort} />
            <SortTh id="can_add_admin" label="زیرمجموعه" sortKey={sortKey} dir={dir} onSort={onSort} />
            <SortTh id="registered" label="ربات" sortKey={sortKey} dir={dir} onSort={onSort} />
            <SortTh id="enforcement_state" label="وضعیت" sortKey={sortKey} dir={dir} onSort={onSort} />
            <SortTh id="exclude_from_billing" label="فاکتور" sortKey={sortKey} dir={dir} onSort={onSort} />
            <TableCell align="left">عملیات</TableCell>
          </TableRow>
        </TableHead>
        <TableBody>
          {tab === 0
            ? rows.map((reseller) => (
              <ResellerTableRow
                key={reseller.id}
                reseller={reseller}
                actions={actions}
                canAddSwitch={canAddSwitch}
              />
            ))
            : visibleTreeRows.map(({ node: reseller, depth }) => (
              <ResellerTableRow
                key={reseller.id}
                reseller={reseller}
                depth={depth}
                tree
                expanded={expanded.has(reseller.id)}
                onToggle={() => onToggleBranch(reseller.id)}
                actions={actions}
                canAddSwitch={canAddSwitch}
              />
            ))}
          {currentCount === 0 && (
            <TableRow>
              <TableCell colSpan={10} align="center" sx={{ py: 7, color: "text.secondary" }}>
                نماینده‌ای با این فیلتر پیدا نشد.
              </TableCell>
            </TableRow>
          )}
        </TableBody>
      </Table>
    </TableContainer>
  );
}
