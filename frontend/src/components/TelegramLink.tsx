import TelegramIcon from "@mui/icons-material/esm/Telegram";
import { IconButton, Tooltip, Typography } from "@mui/material";

// Deep-link to a reseller's Telegram private chat: prefer the public @username link
// (opens reliably in any browser), else fall back to the numeric tg:// link. Returns
// null when neither is known (the reseller never started the bot / has no @username).
export function telegramHref(username?: string | null, chatId?: number | null): string | null {
  if (username) return `https://t.me/${username}`;
  if (chatId) return `tg://user?id=${chatId}`;
  return null;
}

export default function TelegramLink({
  username,
  chatId,
}: {
  username?: string | null;
  chatId?: number | null;
}) {
  const href = telegramHref(username, chatId);
  if (!href) {
    return <Typography variant="caption" color="text.disabled">—</Typography>;
  }
  return (
    <Tooltip title="گفتگو در تلگرام">
      <IconButton
        size="small"
        component="a"
        href={href}
        target="_blank"
        rel="noopener noreferrer"
        sx={{ color: "#229ED9" }}
        aria-label="گفتگوی تلگرام با این نماینده"
      >
        <TelegramIcon fontSize="small" />
      </IconButton>
    </Tooltip>
  );
}
