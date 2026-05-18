import { IconLogout, IconUser } from "@tabler/icons-react";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "./ui/dropdown-menu";

/** vm0-style account block pinned to the bottom of the sidebar.
 *
 * Octopus is single-user, so the only "account" surface is the auth
 * token and a sign-out action. The dropdown gives it visual weight
 * without inventing concepts (orgs, profiles) we don't have. */
export function AccountDropdown({ onSignOut }: { onSignOut: () => void }) {
  // Shorten the auth token for display — it's the only "identity" we have.
  const token =
    typeof localStorage !== "undefined"
      ? localStorage.getItem("octopus_token") || ""
      : "";
  const display = token ? `${token.slice(0, 4)}…${token.slice(-4)}` : "Octopus";
  const initial = (token[0] || "O").toUpperCase();

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          className="btn-account flex w-full items-center gap-3 rounded-lg px-5 py-3 text-left hover:bg-sidebar-accent transition-colors"
          aria-label="Account menu"
        >
          <span className="inline-flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-primary-200 text-primary-700 text-sm font-semibold">
            {initial}
          </span>
          <span className="flex-1 min-w-0">
            <span className="block text-sm font-medium leading-tight truncate text-sidebar-foreground">
              Octopus
            </span>
            <span className="block text-xs leading-tight truncate mt-1.5 text-sidebar-foreground/70 font-mono">
              {display}
            </span>
          </span>
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent side="top" align="start" sideOffset={10} className="w-[260px] p-2">
        <div className="px-3 py-4">
          <div className="flex items-center gap-3">
            <span className="inline-flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-primary-200 text-primary-700 text-sm font-medium">
              {initial}
            </span>
            <div className="flex-1 min-w-0">
              <div className="text-sm font-medium text-foreground truncate">
                Octopus
              </div>
              <div className="text-xs text-muted-foreground truncate font-mono mt-1">
                {display}
              </div>
            </div>
          </div>
        </div>
        <DropdownMenuSeparator />
        <DropdownMenuItem
          className="gap-3 px-3 py-3 rounded-lg"
          onSelect={(e) => {
            e.preventDefault();
            navigator.clipboard?.writeText(token).catch(() => {});
          }}
        >
          <IconUser size={18} stroke={1.5} className="text-muted-foreground" />
          <span>Copy token</span>
        </DropdownMenuItem>
        <DropdownMenuSeparator />
        <DropdownMenuItem
          className="btn-logout gap-3 px-3 py-3 rounded-lg"
          onSelect={onSignOut}
        >
          <IconLogout size={18} stroke={1.5} className="text-muted-foreground" />
          <span>Sign out</span>
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
