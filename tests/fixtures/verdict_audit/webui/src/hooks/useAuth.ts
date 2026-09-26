import { useContext } from "react";
import { AuthContext, AuthState } from "../auth/AuthContext";

/** The signed-in user and the sign-in / sign-out actions; only valid inside an AuthProvider. */
export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) {
    throw new Error("useAuth must be used inside AuthProvider");
  }
  return ctx;
}
