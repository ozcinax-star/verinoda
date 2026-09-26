import { createContext } from "react";

export interface User {
  id: string;
  name: string;
}

export interface AuthState {
  user: User | null;
  signIn: () => Promise<void>;
  signOut: () => Promise<void>;
}

export const AuthContext = createContext<AuthState | null>(null);
