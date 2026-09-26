import { useAuth } from "../hooks/useAuth";

export function ProfileMenu() {
  const auth = useAuth();
  if (!auth.user) {
    return null;
  }
  return (
    <nav className="profile">
      <span>{auth.user.name}</span>
      <button onClick={auth.signOut}>Sign out</button>
    </nav>
  );
}
