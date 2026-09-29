import { useCallback, useEffect, useState } from "react";
import {
  IconCopy,
  IconPlus,
  IconShieldLock,
  IconTrash,
  IconUser,
  IconUserOff,
  IconUserCheck,
} from "@tabler/icons-react";
import type {
  BootstrapResponse,
  IdentityResponse,
  InviteInfo,
  UserInfo,
} from "../api";
import { useSessionStore } from "../stores/sessionStore";
import { PageHeader } from "./PageHeader";
import { Button } from "./ui/button";
import { Input } from "./ui/input";
import { Label } from "./ui/label";

const API = `${window.location.origin}/api/auth`;

/** GET a route, or `null` if it refuses or the server is unreachable.
 *
 * Everything on this page is a read whose failure mode is "show nothing"
 * rather than "throw inside the sidebar" — the auth routes answer 401 to a
 * bearer that has just been replaced, and 403 to the admin sections when you
 * are not one, and neither is worth an error banner.
 */
async function getJson<T>(url: string, headers?: HeadersInit): Promise<T | null> {
  try {
    const res = await fetch(url, headers ? { headers } : undefined);
    return res.ok ? ((await res.json()) as T) : null;
  } catch {
    return null;
  }
}

/** What `POST /api/auth/bootstrap` reports it moved.
 *
 * `BootstrapResponse.summary` is `dict[str, object]` on the wire — the route
 * hands back whatever the upgrade did rather than a fixed record — so the
 * shape it actually contains is stated here, where it is read. */
interface BootstrapSummary {
  user_id?: string;
  username?: string;
  adopted?: Record<string, number>;
  reencrypted?: Record<string, number>;
  extra_roots?: string[];
  agent_memory_moved?: boolean;
}

/** Everything about *who you are* on this install (multi-tenancy.md §3).
 *
 * Three things live here, and which of them you see depends on the install
 * rather than on a setting:
 *
 * * **Claim this install** — only before the first account exists. It takes
 *   the install token, which is the credential you are already signed in with,
 *   so the form asks only for the username and password it is exchanged for.
 *   Doing it here rather than on the login screen means it is done by somebody
 *   already holding the token, not by anybody who can reach a login form.
 * * **Password** — once you are an account. The password is never a bearer, so
 *   changing it revokes every token except the one you are using.
 * * **Invites and people** — only for an admin. An invite code is readable
 *   once, here, for as long as the page is open; the row keeps a digest.
 */
export function AccountPage({ onToggleSidebar }: { onToggleSidebar?: () => void }) {
  const token = useSessionStore((s) => s.token);
  const setToken = useSessionStore((s) => s.setToken);

  const [identity, setIdentity] = useState<IdentityResponse | null>(null);
  const [accountsExist, setAccountsExist] = useState<boolean | null>(null);

  const headers = useCallback(
    () => ({
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
    }),
    [token]
  );

  // Asked again whenever the bearer changes — claiming the install replaces it
  // with the new account's, and the answer to both questions changes with it.
  useEffect(() => {
    let alive = true;
    Promise.all([
      getJson<{ accounts_exist: boolean }>(`${API}/state`),
      getJson<IdentityResponse>(`${API}/identity`, headers()),
    ]).then(([state, who]) => {
      if (!alive) return;
      if (state) setAccountsExist(Boolean(state.accounts_exist));
      if (who) setIdentity(who);
    });
    return () => {
      alive = false;
    };
  }, [headers]);

  return (
    <div className="account-page flex min-h-0 flex-1 flex-col">
      <PageHeader
        className="account-header"
        icon={<IconUser size={17} className="shrink-0 text-gray-700" />}
        crumbs={["Octopus", "Account"]}
        meta={identity?.is_admin ? "admin" : undefined}
        onToggleSidebar={onToggleSidebar}
      />
      <div className="min-h-0 flex-1 overflow-y-auto px-5 py-5">
        <div className="mx-auto flex w-full max-w-2xl flex-col gap-8">
          <IdentityCard identity={identity} accountsExist={accountsExist} />
          {accountsExist === false && (
            <ClaimInstall
              token={token}
              /* Storing the new bearer re-runs the loader above, which is how
               * the page turns from "claim this install" into the account it
               * just created. */
              onClaimed={setToken}
            />
          )}
          {identity?.user_id && <ChangePassword headers={headers} />}
          {identity?.is_admin && <Invites headers={headers} />}
          {identity?.is_admin && <People headers={headers} me={identity.user_id} />}
        </div>
      </div>
    </div>
  );
}

function Section({
  title,
  description,
  actions,
  children,
  className = "",
}: {
  title: string;
  description?: string;
  actions?: React.ReactNode;
  children?: React.ReactNode;
  className?: string;
}) {
  return (
    <section className={`account-section ${className}`}>
      <div className="mb-3 flex items-start gap-3">
        <div className="min-w-0 flex-1">
          <h2 className="text-[15px] font-semibold text-gray-950">{title}</h2>
          {description && (
            <p className="mt-1 text-[12.5px] leading-relaxed text-gray-700">
              {description}
            </p>
          )}
        </div>
        {actions && <div className="shrink-0">{actions}</div>}
      </div>
      {children}
    </section>
  );
}

function IdentityCard({
  identity,
  accountsExist,
}: {
  identity: IdentityResponse | null;
  accountsExist: boolean | null;
}) {
  return (
    <Section
      className="account-identity"
      title={identity?.label || "Signed in"}
      description={
        accountsExist === false
          ? "This install has no accounts yet — you are signed in with its token."
          : identity?.is_admin
            ? "You administer this install: you can invite people and disable accounts."
            : "Your sessions, agents, applications and files are yours alone."
      }
    >
      {identity?.user_id && (
        <p className="font-mono text-[11px] text-gray-600">{identity.user_id}</p>
      )}
    </Section>
  );
}

function ClaimInstall({
  token,
  onClaimed,
}: {
  token: string;
  onClaimed: (token: string) => void;
}) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [summary, setSummary] = useState<BootstrapSummary | null>(null);

  const claim = async () => {
    setBusy(true);
    setError(null);
    try {
      const res = await fetch(`${API}/bootstrap`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({ username, password }),
      });
      const data = await res.json().catch(() => null);
      if (!res.ok) {
        setError(
          typeof data?.detail === "string"
            ? data.detail
            : `Could not create the account — HTTP ${res.status}`
        );
        return;
      }
      const reply = data as BootstrapResponse;
      setSummary((reply.summary ?? {}) as BootstrapSummary);
      onClaimed(reply.token);
    } catch {
      setError("Could not reach Octopus.");
    } finally {
      setBusy(false);
    }
  };

  if (summary) {
    const moved = Object.entries(summary.adopted ?? {});
    return (
      <Section
        className="account-claimed"
        title={`This install now belongs to ${summary.username}`}
        description="The install token no longer signs anybody in. Use your username and password from here on."
      >
        <ul className="space-y-1 text-[12.5px] text-gray-800">
          {moved.length === 0 && <li>Nothing was here to adopt.</li>}
          {moved.map(([table, n]) => (
            <li key={table}>
              <span className="font-mono text-[11.5px]">{table}</span> — {n} row
              {n === 1 ? "" : "s"} adopted
            </li>
          ))}
          {(summary.extra_roots ?? []).length > 0 && (
            <li>
              {summary.extra_roots?.length} existing working director
              {summary.extra_roots?.length === 1 ? "y" : "ies"} stay reachable
            </li>
          )}
          {summary.agent_memory_moved && <li>Agent memory moved into your workspace</li>}
        </ul>
      </Section>
    );
  }

  return (
    <Section
      className="account-claim"
      title="Claim this install"
      description="Create the first account. Everything already here becomes yours, the stored secrets are re-encrypted under your own key, and the install token stops signing anybody in."
    >
      <div className="flex max-w-sm flex-col gap-3">
        <div className="space-y-1.5">
          <Label htmlFor="claim-username">Username</Label>
          <Input
            id="claim-username"
            className="input-claim-username"
            autoCapitalize="off"
            autoCorrect="off"
            spellCheck={false}
            value={username}
            onChange={(e) => setUsername(e.target.value)}
          />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="claim-password">Password</Label>
          <Input
            id="claim-password"
            className="input-claim-password"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") void claim();
            }}
          />
        </div>
        <Button className="btn-claim-install self-start" disabled={busy} onClick={claim}>
          Create account
        </Button>
        {error && <p className="claim-error text-xs text-destructive">{error}</p>}
      </div>
    </Section>
  );
}

function ChangePassword({ headers }: { headers: () => HeadersInit }) {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);

  const submit = async () => {
    setBusy(true);
    setError(null);
    setDone(false);
    try {
      const res = await fetch(`${API}/password`, {
        method: "POST",
        headers: headers(),
        body: JSON.stringify({ current_password: current, new_password: next }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => null);
        setError(
          typeof data?.detail === "string"
            ? data.detail
            : `Could not change the password — HTTP ${res.status}`
        );
        return;
      }
      setCurrent("");
      setNext("");
      setDone(true);
    } catch {
      setError("Could not reach Octopus.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Section
      className="account-password"
      title="Password"
      description="Changing it signs out every other session. This one keeps working."
    >
      <div className="flex max-w-sm flex-col gap-3">
        <div className="space-y-1.5">
          <Label htmlFor="current-password">Current password</Label>
          <Input
            id="current-password"
            className="input-current-password"
            type="password"
            value={current}
            onChange={(e) => setCurrent(e.target.value)}
          />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="new-password">New password</Label>
          <Input
            id="new-password"
            className="input-new-password"
            type="password"
            value={next}
            onChange={(e) => setNext(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") void submit();
            }}
          />
        </div>
        <Button
          className="btn-change-password self-start"
          disabled={busy}
          onClick={submit}
        >
          Change password
        </Button>
        {error && <p className="password-error text-xs text-destructive">{error}</p>}
        {done && (
          <p className="password-done text-xs text-gray-700">Password changed.</p>
        )}
      </div>
    </Section>
  );
}

function Invites({ headers }: { headers: () => HeadersInit }) {
  const [invites, setInvites] = useState<InviteInfo[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Bumped after a create or a revoke; the effect below is the only thing that
  // reads the list, so there is one path onto the screen rather than two.
  const [reload, setReload] = useState(0);

  useEffect(() => {
    let alive = true;
    getJson<InviteInfo[]>(`${API}/invites`, headers()).then((rows) => {
      if (alive && rows) setInvites(rows);
    });
    return () => {
      alive = false;
    };
  }, [headers, reload]);

  const create = async () => {
    setBusy(true);
    setError(null);
    try {
      const res = await fetch(`${API}/invites`, {
        method: "POST",
        headers: headers(),
        body: JSON.stringify({ max_uses: 1, ttl_days: 14 }),
      });
      if (!res.ok) {
        setError(`Could not create an invite — HTTP ${res.status}`);
        return;
      }
      setReload((n) => n + 1);
    } catch {
      setError("Could not reach Octopus.");
    } finally {
      setBusy(false);
    }
  };

  const revoke = async (code: string) => {
    await fetch(`${API}/invites/${encodeURIComponent(code)}`, {
      method: "DELETE",
      headers: headers(),
    }).catch(() => null);
    setReload((n) => n + 1);
  };

  const live = invites.filter((i) => !i.revoked_at);

  return (
    <Section
      className="account-invites"
      title="Invites"
      description="An invite code is one person's way onto this box. It expires in 14 days or when it is used, whichever comes first."
      actions={
        <Button
          size="sm"
          variant="outline"
          className="btn-new-invite"
          disabled={busy}
          onClick={create}
        >
          <IconPlus size={14} />
          New invite
        </Button>
      }
    >
      {live.length === 0 ? (
        <p className="invites-empty text-[12.5px] text-gray-700">
          No live invites.
        </p>
      ) : (
        <ul className="divide-y divide-gray-200 rounded-lg border border-gray-200">
          {live.map((invite) => (
            <li
              key={invite.code}
              className="invite-row flex items-center gap-3 px-3 py-2"
            >
              <code className="invite-code min-w-0 flex-1 truncate font-mono text-[12px] text-gray-900">
                {invite.code}
              </code>
              <span className="shrink-0 text-[11.5px] text-gray-600">
                {invite.used_count}/{invite.max_uses} used
                {invite.expires_at
                  ? ` · expires ${invite.expires_at.slice(0, 10)}`
                  : ""}
              </span>
              <button
                type="button"
                className="btn-copy-invite rounded p-1 text-gray-700 hover:bg-gray-100"
                aria-label="Copy invite code"
                onClick={() =>
                  navigator.clipboard?.writeText(invite.code).catch(() => {})
                }
              >
                <IconCopy size={15} />
              </button>
              <button
                type="button"
                className="btn-revoke-invite rounded p-1 text-gray-700 hover:bg-gray-100"
                aria-label="Revoke invite"
                onClick={() => revoke(invite.code)}
              >
                <IconTrash size={15} />
              </button>
            </li>
          ))}
        </ul>
      )}
      {error && <p className="invites-error mt-2 text-xs text-destructive">{error}</p>}
    </Section>
  );
}

function People({
  headers,
  me,
}: {
  headers: () => HeadersInit;
  me: string | null;
}) {
  const [people, setPeople] = useState<UserInfo[]>([]);
  const [error, setError] = useState<string | null>(null);

  const [reload, setReload] = useState(0);

  useEffect(() => {
    let alive = true;
    getJson<UserInfo[]>(`${API}/users`, headers()).then((rows) => {
      if (alive && rows) setPeople(rows);
    });
    return () => {
      alive = false;
    };
  }, [headers, reload]);

  const setDisabled = async (id: string, disabled: boolean) => {
    const res = await fetch(`${API}/users/${id}/disabled`, {
      method: "POST",
      headers: headers(),
      body: JSON.stringify({ disabled }),
    }).catch(() => null);
    if (res && !res.ok) {
      const data = await res.json().catch(() => null);
      setError(
        typeof data?.detail === "string"
          ? data.detail
          : `Could not update the account — HTTP ${res.status}`
      );
    }
    setReload((n) => n + 1);
  };

  return (
    <Section
      className="account-people"
      title="People"
      description="Disabling an account revokes its tokens and stops it signing in. Nothing it made is deleted."
    >
      <ul className="divide-y divide-gray-200 rounded-lg border border-gray-200">
        {people.map((p) => (
          <li key={p.id} className="person-row flex items-center gap-3 px-3 py-2">
            <span className="min-w-0 flex-1 truncate text-[13px] text-gray-900">
              {p.username}
              {p.is_admin && (
                <IconShieldLock
                  size={13}
                  className="ml-1.5 inline text-gray-600"
                  aria-label="admin"
                />
              )}
            </span>
            <span className="shrink-0 text-[11.5px] text-gray-600">
              {p.disabled_at ? "disabled" : "active"}
            </span>
            {p.id !== me && (
              <button
                type="button"
                className="btn-toggle-disabled rounded p-1 text-gray-700 hover:bg-gray-100"
                aria-label={p.disabled_at ? "Enable account" : "Disable account"}
                onClick={() => setDisabled(p.id, !p.disabled_at)}
              >
                {p.disabled_at ? (
                  <IconUserCheck size={15} />
                ) : (
                  <IconUserOff size={15} />
                )}
              </button>
            )}
          </li>
        ))}
      </ul>
      {error && <p className="people-error mt-2 text-xs text-destructive">{error}</p>}
    </Section>
  );
}
