import { ReactNode, useState } from "react";
import { AuthContext, User } from "./AuthContext";

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const signIn = async () => {
    const res = await fetch("/api/session", { method: "POST" });
    setUser(await res.json());
  };
  const signOut = async () => {
    await fetch("/api/session", { method: "DELETE" });
    setUser(null);
  };
  return <AuthContext.Provider value={{ user, signIn, signOut }}>{children}</AuthContext.Provider>;
}
