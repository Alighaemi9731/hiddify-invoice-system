import { ReactNode } from "react";
import { Box, Chip, Stack, Typography } from "@mui/material";
import { alpha } from "@mui/material/styles";
import { ResellerRow, ResellerTreeRow } from "../../api/client";
import CapacityBar from "../../components/CapacityBar";
import TelegramLink, { telegramHref } from "../../components/TelegramLink";
import { fmtNum } from "../../format";
import { ConnectionStatus, EnforcementStatus, ResellerIdentity } from "./ResellerIdentity";

export default function ResellerMobileCard({
  reseller,
  depth,
  tree,
  expanded,
  onToggle,
  actions,
  canAddSwitch,
}: {
  reseller: ResellerRow | ResellerTreeRow;
  depth: number;
  tree: boolean;
  expanded: boolean;
  onToggle: () => void;
  actions: (reseller: ResellerRow) => ReactNode;
  canAddSwitch: (reseller: ResellerRow) => ReactNode;
}) {
  return (
    <Box
      sx={{
        p: 1.5,
        ms: tree ? Math.min(depth * 1.5, 4) : 0,
        borderRadius: 3,
        border: "1px solid",
        borderColor: "divider",
        bgcolor: (theme) => alpha(theme.palette.background.paper, 0.48),
        borderInlineStartWidth: tree && depth > 0 ? 3 : 1,
        borderInlineStartColor: tree && depth > 0 ? "primary.main" : "divider",
      }}
    >
      <ResellerIdentity
        reseller={reseller}
        depth={depth}
        tree={tree}
        expanded={expanded}
        onToggle={onToggle}
      />
      <Stack direction="row" spacing={0.8} flexWrap="wrap" useFlexGap alignItems="center" sx={{ mt: 1.2 }}>
        {telegramHref(reseller.username, reseller.bot_chat_id) && <TelegramLink username={reseller.username} chatId={reseller.bot_chat_id} />}
        <Chip size="small" label={reseller.panel_key} variant="outlined" />
        <ConnectionStatus connected={reseller.registered} />
        <EnforcementStatus state={reseller.enforcement_state} />
        {reseller.exclude_from_billing && (
          <Chip size="small" color="warning" variant="outlined" label="معاف از فاکتور" />
        )}
      </Stack>
      <Box
        sx={{
          mt: 1.5,
          display: "grid",
          gridTemplateColumns: "1fr 1fr",
          gap: 1.5,
          alignItems: "center",
        }}
      >
        <Box>
          <Typography variant="caption" color="text.secondary">قیمت هر گیگ</Typography>
          <Typography variant="body2" sx={{ fontWeight: 750 }}>
            {fmtNum(reseller.effective_price_per_gb)} تومان
          </Typography>
        </Box>
        <CapacityBar used={reseller.users_count} max={reseller.panel_max_users} />
      </Box>
      <Stack
        direction="row"
        alignItems="center"
        justifyContent="space-between"
        sx={{ mt: 1.2, pt: 1.1, borderTop: 1, borderColor: "divider" }}
      >
        {canAddSwitch(reseller)}
        {actions(reseller)}
      </Stack>
    </Box>
  );
}
