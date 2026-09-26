import { useEffect, useState } from "react";
import {
  IconArchive,
  IconCopy,
  IconLogout,
  IconSettings,
  IconUserCog,
} from "@tabler/icons-react";
import { useSessionStore } from "../stores/sessionStore";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "./ui/dropdown-menu";

/** The account block pinned under the sidebar's hairline.
 *
 * The design gives it an identity row — initial tile, display name, handle in
 * mono — rather than a settings gear, and it stays the single home for
 * app-level actions: there are no gear icons anywhere else in the sidebar.
 *
 * The handle is a **label** (`OCTOPUS_USER_LABEL`, via `/api/auth/identity`),
 * never the token. It used to be the token, on the reasoning that in
 * single-user mode the token is the identity — but the account row is on screen
 * permanently, so that put the credential in every screenshot, screen share and
 * over-the-shoulder glance. Copying it is still one click; *displaying* it is
 * not something the user ever asked for.
 */
export function SidebarAccount({
  onSignOut,
  onOpenSettings,
  onOpenArchivedSessions,
}: {
  onSignOut: () => void;
  onOpenSettings: () => void;
  onOpenArchivedSessions: () => void;
}) {
  const agents = useSessionStore((s) => s.agents);
  const activeAgentId = useSessionStore((s) => s.activeAgentId);
  const openAgentForm = useSessionStore((s) => s.openAgentForm);

  const token = useSessionStore((s) => s.token);
  const [label, setLabel] = useState("");

  // Only this row needs it, so it asks for it here rather than through the
  // store. Re-asked when the token changes, which includes a rotation.
  useEffect(() => {
    if (!token) return;
    let alive = true;
    fetch(`${window.location.origin}/api/auth/identity`, {
      headers: { Authorization: `Bearer ${token}` },
    })
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        if (alive && data && typeof data.label === "string") setLabel(data.label);
      })
      .catch(() => {
        // An unreachable server is already visible everywhere else; the handle
        // just stays generic rather than throwing inside the sidebar.
      });
    return () => {
      alive = false;
    };
  }, [token]);

  const handle = token ? label || "signed in" : "not signed in";
  const initial = ((token && label[0]) || "O").toUpperCase();

  const editActiveAgent = () => {
    const active =
      agents.find((a) => a.id === activeAgentId) ??
      agents.find((a) => a.is_system) ??
      null;
    openAgentForm(active?.id ?? null);
  };

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          className="btn-account flex w-full items-center gap-2.5 rounded-lg px-1.5 py-1 text-left transition-colors hover:bg-gray-100"
          aria-label="Account menu"
        >
          <span className="inline-flex size-7 shrink-0 items-center justify-center rounded-lg bg-primary-100 text-[12px] font-bold text-primary">
            {initial}
          </span>
          <span className="account-text min-w-0 flex-1">
            <span className="block truncate text-[13px] font-semibold leading-tight text-gray-900">
              Octopus
            </span>
            <span className="account-handle mt-0.5 block truncate font-mono text-[10px] leading-tight text-gray-600">
              {handle}
            </span>
          </span>
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" side="top" className="w-56">
        <DropdownMenuItem className="menu-settings" onClick={onOpenSettings}>
          <IconSettings size={15} />
          Settings
        </DropdownMenuItem>
        <DropdownMenuItem className="menu-agent-settings" onClick={editActiveAgent}>
          <IconUserCog size={15} />
          Agent settings
        </DropdownMenuItem>
        <DropdownMenuItem className="menu-archived-sessions" onClick={onOpenArchivedSessions}>
          <IconArchive size={15} />
          Archived sessions
        </DropdownMenuItem>
        <DropdownMenuSeparator />
        <DropdownMenuItem
          onClick={() => navigator.clipboard?.writeText(token).catch(() => {})}
        >
          <IconCopy size={15} />
          Copy token
        </DropdownMenuItem>
        <DropdownMenuItem className="menu-sign-out" onClick={onSignOut}>
          <IconLogout size={15} />
          Sign out
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
