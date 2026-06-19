import { createContext, useContext, useEffect, useState, ReactNode } from "react";
import { getPortalToken, setPortalToken, portalMe, PortalReseller } from "./portalClient";

type PortalAuthState = {
  authed: boolean;
  loading: boolean;
  chatId: number | null;
  resellers: PortalReseller[];
  /** Called by the login page after exchanging the one-time token for a session token. */
  finishLogin: (token: string) => Promise<void>;
  logout: () => void;
};

const Ctx = createContext<PortalAuthState>(null as any);
export const usePortalAuth = () => useContext(Ctx);

export function PortalAuthProvider({ children }: { children: ReactNode }) {
  const [authed, setAuthed] = useState(!!getPortalToken());
  const [loading, setLoading] = useState(true);
  const [chatId, setChatId] = useState<number | null>(null);
  const [resellers, setResellers] = useState<PortalReseller[]>([]);

  useEffect(() => {
    if (getPortalToken()) {
      portalMe()
        .then((m) => { setChatId(m.chat_id); setResellers(m.resellers); setAuthed(true); })
        .catch(() => { setPortalToken(null); setAuthed(false); })
        .finally(() => setLoading(false));
    } else {
      setLoading(false);
    }
  }, []);

  const finishLogin = async (token: string) => {
    setPortalToken(token);
    const m = await portalMe();
    setChatId(m.chat_id);
    setResellers(m.resellers);
    setAuthed(true);
  };

  const logout = () => {
    setPortalToken(null);
    setAuthed(false);
    setChatId(null);
    setResellers([]);
  };

  return (
    <Ctx.Provider value={{ authed, loading, chatId, resellers, finishLogin, logout }}>
      {children}
    </Ctx.Provider>
  );
}
