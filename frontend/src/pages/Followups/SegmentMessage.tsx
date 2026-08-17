import { useEffect, useRef, useState } from "react";
import { Box, Button, Card, Chip, Stack, Typography } from "@mui/material";
import CheckIcon from "@mui/icons-material/esm/Check";
import ContentCopyIcon from "@mui/icons-material/esm/ContentCopy";
import { fmtNum } from "../../format";
import { SEGMENT_MESSAGE_HINTS, segmentMessage } from "./messages";
import { segmentLabel } from "./segments";
import type { CrmSegment } from "../../api/client";

/** Copy with a legacy fallback: `navigator.clipboard` exists only in a secure context, and
 * the panel is reachable over plain http on the server IP (the installer prints that URL),
 * where the modern API is simply absent — the whole point of this card is the copy button. */
async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // permission denied / insecure origin — fall through to the textarea path
  }
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    return ok;
  } catch {
    return false;
  }
}

/**
 * The ready-to-send text for one segment, with a copy button.
 *
 * The owner's real workflow is: filter the board to a bucket, then DM every reseller in it by
 * hand. Nothing here sends anything — Telegram delivery stays manual, exactly like the rest of
 * this board (see `FollowupDialog`'s note). This only saves the retyping.
 *
 * `name` personalizes the greeting when the card sits next to ONE reseller (the drawer). The
 * board omits it: the same text is pasted into many chats.
 */
export default function SegmentMessage({
  segment, count, name, dense,
}: {
  segment: string;
  count?: number;
  name?: string;
  dense?: boolean;
}) {
  const [copied, setCopied] = useState(false);
  const [failed, setFailed] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => () => { if (timer.current) clearTimeout(timer.current); }, []);
  useEffect(() => { setCopied(false); setFailed(false); }, [segment, name]);

  const text = segmentMessage(segment, name);
  if (!text) return null;
  const hint = SEGMENT_MESSAGE_HINTS[segment as CrmSegment];

  const onCopy = async () => {
    const ok = await copyText(text);
    setCopied(ok);
    setFailed(!ok);
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => setCopied(false), 2500);
  };

  return (
    <Card sx={{ p: dense ? 1.5 : 2, mb: dense ? 0 : 2.5 }}>
      <Stack direction={{ xs: "column", sm: "row" }} spacing={1}
        alignItems={{ sm: "center" }} sx={{ mb: 1 }}>
        <Typography sx={{ fontWeight: 700 }}>
          پیام آمادهٔ «{segmentLabel(segment)}»
        </Typography>
        {count != null && count > 0 && (
          <Chip size="small" variant="outlined" label={`${fmtNum(count)} نماینده`} />
        )}
        <Box sx={{ flexGrow: 1 }} />
        <Button size="small" variant={copied ? "outlined" : "contained"}
          color={copied ? "success" : "primary"}
          startIcon={copied ? <CheckIcon /> : <ContentCopyIcon />}
          onClick={onCopy}>
          {copied ? "کپی شد" : "کپی متن پیام"}
        </Button>
      </Stack>

      <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
        {hint} این متن ارسال نمی‌شود؛ کپی کنید و در تلگرام برای نماینده بفرستید.
      </Typography>

      <Box sx={{
        p: 1.5, borderRadius: 1, bgcolor: "action.hover",
        whiteSpace: "pre-wrap", lineHeight: 1.9,
      }}>
        <Typography variant="body2" component="div" sx={{ whiteSpace: "pre-wrap" }}>
          {text}
        </Typography>
      </Box>

      {failed && (
        <Typography variant="body2" color="error" sx={{ mt: 1 }}>
          مرورگر اجازهٔ کپی خودکار نداد — متن بالا را دستی انتخاب و کپی کنید.
        </Typography>
      )}
    </Card>
  );
}
