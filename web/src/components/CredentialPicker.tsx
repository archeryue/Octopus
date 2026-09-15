import { useState } from "react";
import { IconCheck, IconChevronDown } from "@tabler/icons-react";
import { useSessionStore, type SessionInfo } from "../stores/sessionStore";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "./ui/dropdown-menu";

const API = window.location.origin;

/** The header's credential chip, as a control.
 *
 * A session's engine credential used to be fixed at creation: if the sign-in
 * it was bound to lapsed or was deleted, the conversation was stranded with no
 * way back. The chip already said which credential a session was burning, so
 * it's the honest place to change it — pick another sign-in for this engine,
 * or fall back to the agent's.
 *
 * Only credentials matching the session's own backend are offered; the server
 * refuses a mismatch anyway (a Codex sign-in can't run a Claude session).
 */
export function CredentialPicker({ session }: { session: SessionInfo }) {
  const token = useSessionStore((s) => s.token);
  const credentials = useSessionStore((s) => s.credentials);
  const agents = useSessionStore((s) => s.agents);
  const setSessions = useSessionStore((s) => s.setSessions);
  const [busy, setBusy] = useState(false);

  const agent = agents.find((a) => a.id === session.agent_id);
  const usable = credentials.filter((c) => c.backend === session.backend);
  const effectiveId = session.credential_id ?? agent?.credential_id ?? null;
  const effective = credentials.find((c) => c.id === effectiveId);
  const inherited = !session.credential_id && !!agent?.credential_id;

  const choose = async (credentialId: string | null) => {
    if (busy) return;
    setBusy(true);
    try {
      const res = await fetch(`${API}/api/sessions/${session.id}`, {
        method: "PATCH",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({ credential_id: credentialId }),
      });
      if (!res.ok) return;
      const updated: SessionInfo = await res.json();
      const all = useSessionStore.getState().sessions;
      setSessions(all.map((s) => (s.id === updated.id ? updated : s)));
    } finally {
      setBusy(false);
    }
  };

  const label = effective?.label ?? "host default";

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          className="credential-chip pill pill-neutral transition-colors hover:border-primary-100 hover:bg-primary-50"
          title={
            effective
              ? `Credential: ${effective.label}${inherited ? " (from the agent)" : ""}`
              : "No credential attached — using the CLI's own login"
          }
          aria-label="Change this session's credential"
        >
          <span
            className={`size-1.5 rounded-sm ${
              effective?.needs_reconnect ? "bg-warn" : "bg-warn/70"
            }`}
          />
          {label}
          <IconChevronDown size={12} className="opacity-60" />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-64">
        {usable.map((c) => (
          <DropdownMenuItem
            key={c.id}
            className="credential-option"
            onClick={() => choose(c.id)}
          >
            <span className="flex-1 truncate">{c.label}</span>
            {c.needs_reconnect && (
              <span className="font-mono text-[10px] text-warn-foreground">
                expired
              </span>
            )}
            {session.credential_id === c.id && (
              <IconCheck size={14} className="text-primary" />
            )}
          </DropdownMenuItem>
        ))}
        {usable.length === 0 && (
          <DropdownMenuItem disabled>
            No {session.backend} credentials
          </DropdownMenuItem>
        )}
        <DropdownMenuSeparator />
        <DropdownMenuItem
          className="credential-option-inherit"
          onClick={() => choose(null)}
        >
          <span className="flex-1 truncate">
            {agent?.credential_id
              ? `Agent's credential (${agent.name})`
              : "Host default sign-in"}
          </span>
          {!session.credential_id && <IconCheck size={14} className="text-primary" />}
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
