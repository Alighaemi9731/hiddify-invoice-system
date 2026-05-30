import { Component, ReactNode } from "react";
import { Box, Button, Typography, Paper } from "@mui/material";
import ErrorOutlineIcon from "@mui/icons-material/ErrorOutline";

type Props = { children: ReactNode };
type State = { error: Error | null };

// Catches render errors in any page so the app shows a friendly message
// (and a reload button) instead of a blank white screen.
export default class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: unknown) {
    // eslint-disable-next-line no-console
    console.error("UI error:", error, info);
  }

  render() {
    if (this.state.error) {
      return (
        <Box sx={{ p: 3, display: "grid", placeItems: "center", minHeight: "60vh" }}>
          <Paper sx={{ p: 4, maxWidth: 520, textAlign: "center" }}>
            <ErrorOutlineIcon color="error" sx={{ fontSize: 48, mb: 1 }} />
            <Typography variant="h6" sx={{ mb: 1 }}>خطایی در این صفحه رخ داد</Typography>
            <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
              این بخش به‌درستی بارگذاری نشد. می‌توانید صفحه را دوباره بارگذاری کنید.
            </Typography>
            <Typography variant="caption" color="text.secondary" dir="ltr"
              sx={{ display: "block", mb: 2, wordBreak: "break-word" }}>
              {String(this.state.error?.message || this.state.error)}
            </Typography>
            <Button variant="contained" onClick={() => { this.setState({ error: null }); location.reload(); }}>
              بارگذاری مجدد
            </Button>
          </Paper>
        </Box>
      );
    }
    return this.props.children;
  }
}
