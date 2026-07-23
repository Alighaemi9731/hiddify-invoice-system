import { useState } from "react";
import {
  Alert, Box, Button, Card, CardContent, Chip, Dialog, DialogActions, DialogContent, DialogTitle,
  LinearProgress, MenuItem, Stack, TextField, Typography,
} from "@mui/material";
import SendIcon from "@mui/icons-material/esm/Send";
import { useInfiniteQuery, useQuery, useQueryClient } from "@tanstack/react-query";
import { useOutletContext } from "react-router-dom";
import { DataState } from "../../components/DataState";
import { fmtDateTime, fmtNum } from "../../format";
import {
  cancelBroadcast, createBroadcast, getBroadcast, listBroadcasts, previewAudience, storefrontQueryKeys,
} from "./api";
import type { StorefrontOutletContext } from "./StorefrontShell";
import type { AudienceSegment, BroadcastCreateResult, BroadcastJob, Versioned } from "./types";
import { useIdempotentMutation } from "./mutation";
import { useXsFullScreen } from "../../responsive";

const SEGMENT_FA: Record<AudienceSegment, string> = {
  all: "همهٔ مشتری‌ها",
  expired: "منقضی‌شده‌ها",
  inactive30: "۳۰ روز غیرفعال",
  trial_no_purchase: "تست‌گرفته‌های بدونِ خرید",
};
const STATUS_FA: Record<string, string> = {
  queued: "در صف", running: "در حال ارسال", completed: "کامل شد", canceled: "لغو شد",
};
const statusColor = (s: string): "default" | "info" | "success" | "warning" =>
  s === "completed" ? "success" : s === "canceled" ? "warning" : s === "running" ? "info" : "default";

const MAX_TEXT = 4000;

export default function StorefrontCampaignsPage() {
  const { shop } = useOutletContext<StorefrontOutletContext>();
  const queryClient = useQueryClient();
  const [segment, setSegment] = useState<AudienceSegment>("all");
  const [text, setText] = useState("");
  const [trackJobId, setTrackJobId] = useState<number | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const audienceQuery = useQuery({
    queryKey: storefrontQueryKeys.audience(shop.id, segment),
    queryFn: () => previewAudience(shop.id, segment),
  });

  const historyQuery = useInfiniteQuery({
    queryKey: storefrontQueryKeys.broadcasts(shop.id),
    queryFn: ({ pageParam }) => listBroadcasts(shop.id, pageParam),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
  const jobs = historyQuery.data?.pages.flatMap((p) => p.items) ?? [];

  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: ["portal-storefronts", shop.id, "broadcasts"] });

  const sendMut = useIdempotentMutation<Versioned<BroadcastCreateResult>, { segment: AudienceSegment; text: string }>(
    (v, key) => createBroadcast(shop.id, v.segment, v.text, key),
    {
      onSuccess: async (result) => {
        await invalidate();
        setText("");
        setTrackJobId(result.data.job_id);
        setMessage(`پیام برای ${fmtNum(result.data.total)} نفر در صف ارسال قرار گرفت.`);
      },
    },
  );

  const audience = audienceQuery.data;
  const textLen = text.trim().length;
  const canSend = !sendMut.isPending && textLen >= 1 && textLen <= MAX_TEXT
    && !!audience && audience.count > 0 && !audience.over_cap;

  return (
    <Box>
      <Stack spacing={0.5} sx={{ mb: 2 }}>
        <Typography variant="h6">پیام‌رسانی به مشتریان</Typography>
        <Typography variant="body2" color="text.secondary">
          پیام همگانی به یک گروه از مشتریان بفرستید. ارسال در صف قرار می‌گیرد و به‌تدریج انجام می‌شود.
        </Typography>
      </Stack>

      {message && <Alert severity="success" onClose={() => setMessage(null)} sx={{ mb: 2 }}>{message}</Alert>}
      {sendMut.isError && <Alert severity="error" sx={{ mb: 2 }}>ارسال ناموفق بود. ورودی‌ها را بررسی کنید.</Alert>}

      <Card sx={{ mb: 2 }}>
        <CardContent>
          <Stack spacing={2}>
            <Stack direction={{ xs: "column", sm: "row" }} spacing={1.5} alignItems={{ sm: "center" }}>
              <TextField select size="small" label="گیرندگان" value={segment}
                onChange={(e) => setSegment(e.target.value as AudienceSegment)}
                sx={{ minWidth: { xs: "100%", sm: 220 } }}>
                {Object.entries(SEGMENT_FA).map(([value, label]) => (
                  <MenuItem key={value} value={value}>{label}</MenuItem>
                ))}
              </TextField>
              <Typography variant="body2" color="text.secondary">
                {audienceQuery.isLoading ? "در حال شمارش…"
                  : audience ? `${fmtNum(audience.count)} گیرنده` : "—"}
              </Typography>
            </Stack>
            {audience && audience.over_cap && (
              <Alert severity="warning">تعداد گیرندگان از حد مجاز بیشتر است؛ گروه کوچک‌تری انتخاب کنید.</Alert>
            )}
            {audience && audience.sample.length > 0 && (
              <Typography variant="caption" color="text.secondary">
                نمونه: {audience.sample.slice(0, 8).map((c) =>
                  c.name?.trim() || (c.username ? `@${c.username}` : `#${c.telegram_id}`)).join("، ")}
                {audience.count > audience.sample.length ? " …" : ""}
              </Typography>
            )}
            <TextField label="متن پیام" value={text} onChange={(e) => setText(e.target.value)}
              multiline minRows={3} fullWidth
              error={textLen > MAX_TEXT}
              helperText={`${fmtNum(textLen)} / ${fmtNum(MAX_TEXT)} نویسه`} />
            <Box>
              <Button variant="contained" startIcon={<SendIcon />} disabled={!canSend}
                onClick={() => sendMut.mutate({ segment, text: text.trim() })}>
                {sendMut.isPending ? "در حال ثبت…" : "ارسال"}
              </Button>
            </Box>
            <Alert severity="info" sx={{ mt: 0 }}>
              ارسال «حداقل یک‌بار» است؛ در موارد نادر (مانندِ قطعیِ ارتباط در میانهٔ ارسال) ممکن است یک پیام دوبار برای یک مشتری ارسال شود.
            </Alert>
          </Stack>
        </CardContent>
      </Card>

      <Typography variant="subtitle1" sx={{ mb: 1 }}>تاریخچهٔ کمپین‌ها</Typography>
      <DataState isLoading={historyQuery.isLoading} isError={historyQuery.isError} rows={4}
        onRetry={() => historyQuery.refetch()}>
        {!jobs.length ? (
          <Alert severity="info">هنوز کمپینی ارسال نشده است.</Alert>
        ) : (
          <Stack spacing={1.25}>
            {jobs.map((job) => (
              <CampaignRow key={job.id} job={job} onTrack={() => setTrackJobId(job.id)} />
            ))}
            {historyQuery.hasNextPage && (
              <Button variant="outlined" onClick={() => historyQuery.fetchNextPage()}
                disabled={historyQuery.isFetchingNextPage} sx={{ alignSelf: "center", mt: 1 }}>
                {historyQuery.isFetchingNextPage ? "در حال بارگذاری…" : "نمایش بیشتر"}
              </Button>
            )}
          </Stack>
        )}
      </DataState>

      {trackJobId != null && (
        <CampaignProgressDialog shopId={shop.id} jobId={trackJobId}
          onClose={() => setTrackJobId(null)} onChanged={() => void invalidate()} />
      )}
    </Box>
  );
}

function CampaignRow({ job, onTrack }: { job: BroadcastJob; onTrack: () => void }) {
  const done = job.total > 0 ? Math.round(((job.sent + job.blocked + job.failed) / job.total) * 100) : 0;
  return (
    <Card>
      <CardContent sx={{ "&:last-child": { pb: 2 } }}>
        <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap>
          <Chip size="small" variant="outlined" color={statusColor(job.status)}
            label={STATUS_FA[job.status] || job.status} />
          {job.kind === "direct"
            ? <Chip size="small" variant="outlined" label="مستقیم" />
            : <Chip size="small" variant="outlined" label={SEGMENT_FA[(job.segment ?? "all") as AudienceSegment] || job.segment} />}
          <Typography variant="caption" color="text.secondary">
            {job.created_at ? fmtDateTime(job.created_at) : ""}
          </Typography>
        </Stack>
        <Typography variant="body2" sx={{ mt: 0.75, whiteSpace: "pre-wrap" }} noWrap>{job.text}</Typography>
        <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 0.5 }}>
          ارسال‌شده {fmtNum(job.sent)} · مسدود {fmtNum(job.blocked)} · ناموفق {fmtNum(job.failed)} · باقی {fmtNum(job.pending)} از {fmtNum(job.total)}
        </Typography>
        <LinearProgress variant="determinate" value={done} sx={{ mt: 1, borderRadius: 1 }} />
        <Button size="small" sx={{ mt: 1 }} onClick={onTrack}>جزئیات و مدیریت</Button>
      </CardContent>
    </Card>
  );
}

function CampaignProgressDialog({
  shopId, jobId, onClose, onChanged,
}: { shopId: number; jobId: number; onClose: () => void; onChanged: () => void }) {
  const xsFull = useXsFullScreen();
  const queryClient = useQueryClient();
  const jobQuery = useQuery({
    queryKey: storefrontQueryKeys.broadcast(shopId, jobId),
    queryFn: () => getBroadcast(shopId, jobId),
    refetchInterval: (query) => {
      const s = query.state.data?.status;
      return s === "queued" || s === "running" ? 3000 : false;
    },
  });
  const cancelMut = useIdempotentMutation<Versioned<BroadcastJob>, Record<string, never>>(
    (_v, key) => cancelBroadcast(shopId, jobId, key),
    {
      onSuccess: async () => {
        await queryClient.invalidateQueries({ queryKey: storefrontQueryKeys.broadcast(shopId, jobId) });
        onChanged();
      },
    },
  );
  const job = jobQuery.data;
  const cancellable = job && (job.status === "queued" || job.status === "running");

  return (
    <Dialog open onClose={onClose} maxWidth="sm" fullWidth fullScreen={xsFull}>
      <DialogTitle>کمپین #{fmtNum(jobId)}</DialogTitle>
      <DialogContent dividers>
        <DataState isLoading={jobQuery.isLoading} isError={jobQuery.isError} rows={2}
          onRetry={() => jobQuery.refetch()}>
          {job && (
            <Stack spacing={1.25}>
              <Chip size="small" variant="outlined" color={statusColor(job.status)}
                label={STATUS_FA[job.status] || job.status} sx={{ alignSelf: "flex-start" }} />
              <Typography variant="body2" sx={{ whiteSpace: "pre-wrap" }}>{job.text}</Typography>
              <Stack direction="row" spacing={2} flexWrap="wrap" useFlexGap>
                <Metric label="کل" value={job.total} />
                <Metric label="ارسال‌شده" value={job.sent} />
                <Metric label="مسدود" value={job.blocked} />
                <Metric label="ناموفق" value={job.failed} />
                <Metric label="باقی‌مانده" value={job.pending} />
              </Stack>
              {cancelMut.isError && <Alert severity="error">لغو ناموفق بود.</Alert>}
            </Stack>
          )}
        </DataState>
      </DialogContent>
      <DialogActions>
        {cancellable && (
          <Button color="warning" disabled={cancelMut.isPending} onClick={() => cancelMut.mutate({})}>
            لغو ارسال‌نشده‌ها
          </Button>
        )}
        <Button variant="contained" onClick={onClose}>بستن</Button>
      </DialogActions>
    </Dialog>
  );
}

function Metric({ label, value }: { label: string; value: number }) {
  return (
    <Box sx={{ minWidth: 80 }}>
      <Typography variant="caption" color="text.secondary">{label}</Typography>
      <Typography variant="h6">{fmtNum(value)}</Typography>
    </Box>
  );
}
