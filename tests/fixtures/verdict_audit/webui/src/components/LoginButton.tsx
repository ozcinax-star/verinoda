import { useAuth } from "../hooks/useAuth";

export function LoginButton() {
  const { user, signIn } = useAuth();
  if (user) {
    return <span className="user">{user.name}</span>;
  }
  return <button onClick={signIn}>Sign in</button>;
}
