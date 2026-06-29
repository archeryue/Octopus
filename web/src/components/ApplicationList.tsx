import { IconPlus } from "@tabler/icons-react";

/** Sidebar "Applications" section. Houses applications managed by this
 * Octopus instance. The + button opens the add-application flow (wired up
 * by the parent via `onAdd`). */
export function ApplicationList({ onAdd }: { onAdd: () => void }) {
  return (
    <div className="application-section shrink-0">
      <div className="application-header group flex h-8 items-center justify-between rounded-lg px-2 hover:bg-sidebar-accent transition-colors">
        <h2 className="text-[13px] font-medium leading-4 text-sidebar-foreground/50 group-hover:text-sidebar-foreground transition-colors uppercase tracking-wide">
          Applications
        </h2>
        <button
          className="btn-application-add inline-flex h-6 w-6 items-center justify-center rounded-md text-sidebar-foreground/70 hover:bg-[hsl(var(--gray-200))] hover:text-sidebar-foreground transition-colors"
          onClick={onAdd}
          title="Add application"
          aria-label="Add application"
        >
          <IconPlus size={14} />
        </button>
      </div>
    </div>
  );
}
