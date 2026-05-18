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
  // The token IS the "username" for single-user mode. Show it whole so
  // the user can scan it; CSS truncate only kicks in for tokens longer
  // than the trigger row can fit.
  const token =
    typeof localStorage !== "undefined"
      ? localStorage.getItem("octopus_token") || ""
      : "";
  const display = token || "Octopus";
  const initial = (token[0] || "O").toUpperCase();

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          className="btn-account flex w-full items-center gap-2 rounded-lg px-1.5 py-1.5 text-left hover:bg-sidebar-accent transition-colors"
          aria-label="Account menu"
        >
          <span className="inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-primary-200 text-primary-700 text-xs font-semibold">
            {initial}
          </span>
          <span className="flex-1 min-w-0">
            <span className="block text-sm font-medium leading-tight truncate text-sidebar-foreground">
              Octopus
            </span>
            <span className="block text-xs leading-tight truncate mt-0.5 text-sidebar-foreground/70 font-mono">
              {display}
            </span>
          </span>
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent side="top" align="start" sideOffset={6} className="w-[260px] p-1">
        <div className="px-2 py-2">
          <div className="flex items-center gap-2.5">
            <span className="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-primary-200 text-primary-700 text-sm font-medium">
              {initial}
            </span>
            <div className="flex-1 min-w-0">
              <div className="text-sm font-medium text-foreground truncate">
                Octopus
              </div>
              <div className="text-xs text-muted-foreground truncate font-mono mt-0.5">
                {display}
              </div>
            </div>
          </div>
        </div>
        <DropdownMenuSeparator />
        <DropdownMenuItem
          className="gap-2 px-2 py-2 rounded-md"
          onSelect={(e) => {
            e.preventDefault();
            navigator.clipboard?.writeText(token).catch(() => {});
          }}
        >
          <IconUser size={16} stroke={1.5} className="text-muted-foreground" />
          <span>Copy token</span>
        </DropdownMenuItem>
        <DropdownMenuSeparator />
        <DropdownMenuItem
          className="btn-logout gap-2 px-2 py-2 rounded-md"
          onSelect={onSignOut}
        >
          <IconLogout size={16} stroke={1.5} className="text-muted-foreground" />
          <span>Sign out</span>
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
