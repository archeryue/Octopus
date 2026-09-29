import { useEffect, useState, type KeyboardEvent } from "react";
import type { AuthStateResponse, LoginResponse } from "../api";
import { Button } from "./ui/button";
import { Input } from "./ui/input";
import { Label } from "./ui/label";

const API = window.location.origin;

/** The way in, for both eras of this install (multi-tenancy.md §9).
 *
 * Before the first account exists Octopus is the single-user install it has
 * always been, the way in is its own token, and that is the only thing this
 * screen asks for — offering a username field to somebody who has no account
 * would be asking a question with no answer. Once an account exists it asks
 * for a username and a password, and an invite code opens a second form.
 *
 * The screen learns which era it is in from `GET /api/auth/state`, the one
 * thing a signed-out caller may know. It defaults to the token form: that is
 * the form that works on an install nobody has set up yet, and it is also the
 * one the existing e2e suite drives.
 *
 * Claiming the install — turning it into its first account — is deliberately
 * *not* here. It needs the install token, so the person doing it can already
 * sign in; it belongs on the Account page, one step further in, where it is
 * done by somebody who is already holding the credential rather than by
 * anybody who can reach the login screen.
 */

type Mode = "token" | "login" | "register";

export function SignIn({
  /** Called with the bearer to store once the server has issued one. */
  onSignedIn,
}: {
  onSignedIn: (token: string) => void;
}) {
  const [mode, setMode] = useState<Mode>("token");
  const [token, setToken] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [invite, setInvite] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let alive = true;
    fetch(`${API}/api/auth/state`)
      .then((r) => (r.ok ? (r.json() as Promise<AuthStateResponse>) : null))
      .then((d) => {
        if (alive && d?.accounts_exist) setMode("login");
      })
      .catch(() => {
        // An unreachable server leaves the token form up, which is the right
        // guess for an install that has not been set up.
      });
    return () => {
      alive = false;
    };
  }, []);

  const post = async (path: string, body: object) => {
    setBusy(true);
    setError(null);
    try {
      const res = await fetch(`${API}${path}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await res.json().catch(() => null);
      if (!res.ok) {
        const detail = data?.detail;
        setError(
          typeof detail === "string" && detail.trim()
            ? detail
            : `Could not sign in — HTTP ${res.status}`
        );
        return null;
      }
      return data as LoginResponse;
    } catch {
      setError("Could not reach Octopus.");
      return null;
    } finally {
      setBusy(false);
    }
  };

  // Pre-accounts: the install token *is* the bearer, so there is nothing to
  // exchange it for. Stored as it was typed, exactly as before.
  const submitToken = () => {
    if (token.trim()) onSignedIn(token.trim());
  };

  const submitLogin = async () => {
    const data = await post("/api/auth/login", { username, password });
    if (data?.token) onSignedIn(data.token);
  };

  const submitRegister = async () => {
    const data = await post("/api/auth/register", {
      invite_code: invite,
      username,
      password,
    });
    if (data?.token) onSignedIn(data.token);
  };

  return (
    <div className="login-screen flex min-h-screen items-center justify-center bg-gray-50 p-6">
      <div className="w-full max-w-sm rounded-2xl border border-gray-300 bg-card p-8 shadow-[0_24px_60px_-28px_rgba(28,44,72,0.28)]">
        <h1 className="mb-6 text-2xl font-bold tracking-tight text-gray-950">
          Octopus
        </h1>

        {mode === "token" && (
          <>
            <p className="mb-6 text-sm leading-relaxed text-gray-800">
              Enter your access token to continue.
            </p>
            <div className="space-y-4">
              <SignInField
                id="token"
                label="Token"
                type="password"
                value={token}
                onChange={setToken}
                onEnter={submitToken}
                placeholder="Paste your token"
                autoFocus
              />
              <Button className="btn-login w-full" onClick={submitToken}>
                Connect
              </Button>
            </div>
          </>
        )}

        {mode === "login" && (
          <>
            <p className="mb-6 text-sm leading-relaxed text-gray-800">
              Sign in to continue.
            </p>
            <div className="space-y-4">
              <SignInField
                id="username"
                label="Username"
                value={username}
                onChange={setUsername}
                onEnter={submitLogin}
                autoFocus
              />
              <SignInField
                id="password"
                label="Password"
                type="password"
                value={password}
                onChange={setPassword}
                onEnter={submitLogin}
              />
              <Button
                className="btn-login w-full"
                disabled={busy}
                onClick={submitLogin}
              >
                Sign in
              </Button>
              <button
                type="button"
                className="btn-have-invite w-full text-center text-xs text-gray-700 hover:underline"
                onClick={() => {
                  setError(null);
                  setMode("register");
                }}
              >
                I have an invite code
              </button>
            </div>
          </>
        )}

        {mode === "register" && (
          <>
            <p className="mb-6 text-sm leading-relaxed text-gray-800">
              Join this Octopus with an invite code.
            </p>
            <div className="space-y-4">
              <SignInField
                id="invite"
                label="Invite code"
                value={invite}
                onChange={setInvite}
                onEnter={submitRegister}
                autoFocus
              />
              <SignInField
                id="username"
                label="Choose a username"
                value={username}
                onChange={setUsername}
                onEnter={submitRegister}
              />
              <SignInField
                id="password"
                label="Choose a password"
                type="password"
                value={password}
                onChange={setPassword}
                onEnter={submitRegister}
              />
              <Button
                className="btn-register w-full"
                disabled={busy}
                onClick={submitRegister}
              >
                Create account
              </Button>
              <button
                type="button"
                className="btn-back-to-sign-in w-full text-center text-xs text-gray-700 hover:underline"
                onClick={() => {
                  setError(null);
                  setMode("login");
                }}
              >
                Back to sign in
              </button>
            </div>
          </>
        )}

        {error && (
          <p className="signin-error mt-4 text-xs text-destructive">{error}</p>
        )}
      </div>
    </div>
  );
}

function SignInField({
  id,
  label,
  value,
  onChange,
  onEnter,
  type = "text",
  placeholder,
  autoFocus = false,
}: {
  id: string;
  label: string;
  value: string;
  onChange: (v: string) => void;
  onEnter: () => void;
  type?: string;
  placeholder?: string;
  autoFocus?: boolean;
}) {
  return (
    <div className="space-y-2">
      <Label htmlFor={id}>{label}</Label>
      <Input
        id={id}
        type={type}
        autoCapitalize="off"
        autoCorrect="off"
        spellCheck={false}
        enterKeyHint="go"
        value={value}
        placeholder={placeholder}
        autoFocus={autoFocus}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e: KeyboardEvent) => {
          if (e.key === "Enter") onEnter();
        }}
      />
    </div>
  );
}
