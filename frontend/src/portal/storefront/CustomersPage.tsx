import { useMemo, useState } from "react";
import {
  Alert, Box, Button, Card, CardActionArea, CardContent, Chip, MenuItem, Stack,
  TextField, Typography,
} from "@mui/material";
import SearchIcon from "@mui/icons-material/esm/Search";
import BlockIcon from "@mui/icons-material/esm/Block";
import { useInfiniteQuery } from "@tanstack/react-query";
import { useNavigate, useOutletContext } from "react-router-dom";
import { DataState } from "../../components/DataState";
import { fmtDateTime, fmtNum, fmtToman } from "../../format";
import { listCustomers, storefrontQueryKeys } from "./api";
import type { StorefrontOutletContext } from "./StorefrontShell";
import type { CustomerListFilters, CustomerListItem } from "./types";

type BannedFilter = "all" | "true" | "false";
type ActivityFilter = "all" | "active30" | "inactive30";
type ServiceFilter = "all" | "true" | "false";

// A query is billable to the server only when it is either an exact numeric telegram-id or at
// least 3 characters of a name; shorter free text is ignored locally so we never trigger a 422.
const isQueryUsable = (q: string) => q.length === 0 || /^\d+$/.test(q) || q.length >= 3;

export default function CustomersPage() {
  const { shop } = useOutletContext<StorefrontOutletContext>();
  const navigate = useNavigate();
  const [q, setQ] = useState("");
  const [banned, setBanned] = useState<BannedFilter>("all");
  const [activity, setActivity] = useState<ActivityFilter>("all");
  const [service, setService] = useState<ServiceFilter>("all");

  const trimmedQ = q.trim();
  const queryUsable = isQueryUsable(trimmedQ);
  const filters = useMemo<CustomerListFilters>(() => ({
    q: queryUsable && trimmedQ ? trimmedQ : undefined,
    banned: banned === "all" ? undefined : banned === "true",
    activity: activity === "all" ? undefined : activity,
    has_service: service === "all" ? undefined : service === "true",
  }), [queryUsable, trimmedQ, banned, activity, service]);

  const query = useInfiniteQuery({
    queryKey: storefrontQueryKeys.customers(shop.id, filters),
    queryFn: ({ pageParam }) => listCustomers(shop.id, filters, pageParam),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
  });

  const customers = query.data?.pages.flatMap((page) => page.items) ?? [];

  return (
    <Box>
      <Stack spacing={0.5} sx={{ mb: 2 }}>
        <Typography variant="h6">مشتریان</Typography>
        <Typography variant="body2" color="text.secondary">
          مشتریان فروشگاه را جست‌وجو، فیلتر و مدیریت کنید.
        </Typography>
      </Stack>

      <Card sx={{ mb: 2 }}>
        <CardContent>
          <Stack direction={{ xs: "column", md: "row" }} spacing={1.5} alignItems={{ md: "flex-start" }}>
            <TextField
              fullWidth
              size="small"
              label="جست‌وجو (نام یا شناسهٔ تلگرام)"
              value={q}
              onChange={(event) => setQ(event.target.value)}
              error={!!trimmedQ && !queryUsable}
              helperText={!!trimmedQ && !queryUsable
                ? "برای جست‌وجوی نام حداقل ۳ نویسه یا یک شناسهٔ عددی وارد کنید."
                : " "}
              InputProps={{ startAdornment: <SearchIcon color="disabled" sx={{ mr: 1 }} /> }}
            />
            <TextField
              select size="small" label="وضعیت مسدودی" value={banned}
              onChange={(event) => setBanned(event.target.value as BannedFilter)}
              sx={{ minWidth: { xs: "100%", md: 150 } }}
            >
              <MenuItem value="all">همه</MenuItem>
              <MenuItem value="false">مسدودنشده</MenuItem>
              <MenuItem value="true">مسدود</MenuItem>
            </TextField>
            <TextField
              select size="small" label="فعالیت" value={activity}
              onChange={(event) => setActivity(event.target.value as ActivityFilter)}
              sx={{ minWidth: { xs: "100%", md: 170 } }}
            >
              <MenuItem value="all">همه</MenuItem>
              <MenuItem value="active30">فعال در ۳۰ روز</MenuItem>
              <MenuItem value="inactive30">غیرفعال در ۳۰ روز</MenuItem>
            </TextField>
            <TextField
              select size="small" label="سرویس" value={service}
              onChange={(event) => setService(event.target.value as ServiceFilter)}
              sx={{ minWidth: { xs: "100%", md: 150 } }}
            >
              <MenuItem value="all">همه</MenuItem>
              <MenuItem value="true">دارای سرویس</MenuItem>
              <MenuItem value="false">بدون سرویس</MenuItem>
            </TextField>
          </Stack>
        </CardContent>
      </Card>

      <DataState
        isLoading={query.isLoading}
        isError={query.isError}
        rows={6}
        onRetry={() => query.refetch()}
      >
        {!customers.length ? (
          <Alert severity="info">مشتری‌ای با این فیلترها یافت نشد.</Alert>
        ) : (
          <Stack spacing={1.25}>
            {customers.map((customer) => (
              <CustomerRow
                key={customer.id}
                customer={customer}
                onOpen={() => navigate(`/portal/storefront/${shop.id}/customers/${customer.id}`)}
              />
            ))}
            {query.hasNextPage && (
              <Button
                variant="outlined"
                onClick={() => query.fetchNextPage()}
                disabled={query.isFetchingNextPage}
                sx={{ alignSelf: "center", mt: 1 }}
              >
                {query.isFetchingNextPage ? "در حال بارگذاری…" : "نمایش بیشتر"}
              </Button>
            )}
          </Stack>
        )}
      </DataState>
    </Box>
  );
}

function CustomerRow({ customer, onOpen }: { customer: CustomerListItem; onOpen: () => void }) {
  const title = customer.name?.trim()
    || (customer.username ? `@${customer.username.replace(/^@/, "")}` : `کاربر ${fmtNum(customer.telegram_id)}`);
  return (
    <Card>
      <CardActionArea onClick={onOpen} aria-label={`باز کردن مشتری ${title}`}>
        <CardContent sx={{ "&:last-child": { pb: 2 } }}>
          <Stack
            direction={{ xs: "column", sm: "row" }}
            spacing={1.5}
            alignItems={{ xs: "flex-start", sm: "center" }}
            justifyContent="space-between"
          >
            <Box sx={{ minWidth: 0 }}>
              <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap">
                <Typography sx={{ fontWeight: 800 }} noWrap>{title}</Typography>
                {customer.banned && (
                  <Chip size="small" color="error" variant="outlined" icon={<BlockIcon />} label="مسدود" />
                )}
                {customer.has_service && (
                  <Chip size="small" color="success" variant="outlined" label="دارای سرویس" />
                )}
                {customer.free_trial_used && (
                  <Chip size="small" variant="outlined" label="تست استفاده‌شده" />
                )}
              </Stack>
              <Typography variant="caption" color="text.secondary" dir="ltr" sx={{ display: "block", textAlign: "right" }}>
                #{fmtNum(customer.telegram_id)}
                {customer.username ? ` · @${customer.username.replace(/^@/, "")}` : ""}
              </Typography>
            </Box>
            <Stack spacing={0.25} alignItems={{ xs: "flex-start", sm: "flex-end" }}>
              <Typography variant="body2" sx={{ fontWeight: 800, whiteSpace: "nowrap" }}>
                {fmtToman(customer.wallet_balance_toman)}
              </Typography>
              <Typography variant="caption" color="text.secondary">
                آخرین فعالیت: {fmtDateTime(customer.last_seen_at)}
              </Typography>
            </Stack>
          </Stack>
        </CardContent>
      </CardActionArea>
    </Card>
  );
}
