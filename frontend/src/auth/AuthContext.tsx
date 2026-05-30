import { createContext, useContext, useEffect, useState, ReactNode } from "react";
import { getToken, setToken, getMe } from "../api/client";

type AuthState = {
  authed: boolean;
  username: string | null;
  loading: boolean;
  /** Called by the Login page after it obtains a token (captcha/2FA handled there). */
  finishLogin: (token: string) => Promise<void>;
  logout: () => void;
};

const Ctx = createContext<AuthState>(null as any);
export const useAuth = () => useContext(Ctx);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [authed, setAuthed] = useState(!!getToken());
  const [username, setUsername] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (getToken()) {
      getMe()
        .then((u) => { setUsername(u.username); setAuthed(true); })
        .catch(() => { setToken(null); setAuthed(false); })
        .finally(() => setLoading(false));
    } else {
      setLoading(false);
    }
  }, []);

  const finishLogin = async (token: string) => {
    setToken(token);
    const me = await getMe();
    setUsername(me.username);
    setAuthed(true);
  };

  const logout = () => {
    setToken(null);
    setAuthed(false);
    setUsername(null);
  };

  return (
    <Ctx.Provider value={{ authed, username, loading, finishLogin, logout }}>
      {children}
    </Ctx.Provider>
  );
}
