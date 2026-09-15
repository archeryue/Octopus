import type { ReactNode } from "react";

/** The sidebar's section label — "AGENTS", "APPLICATIONS", "MANAGE".
 *
 * Set in mono at 11px with wide tracking, per the console design: the labels
 * read as system chrome rather than content, which is what lets the agent and
 * session rows below them carry the visual weight. The optional action is the
 * small "+" that sits flush right on its baseline.
 */
export function SidebarSectionHeader({
  label,
  className = "",
  action,
}: {
  label: string;
  className?: string;
  action?: {
    icon: ReactNode;
    onClick: () => void;
    title: string;
    label: string;
    className?: string;
  };
}) {
  return (
    <div
      className={`sidebar-section-header flex items-center justify-between px-2 pb-1.5 pt-2 ${className}`}
    >
      <h2 className="font-mono text-[11px] uppercase tracking-[0.12em] text-gray-700">
        {label}
      </h2>
      {action && (
        <button
          type="button"
          className={`inline-flex h-[18px] w-[18px] items-center justify-center rounded-md text-gray-600 transition-colors hover:bg-gray-200 hover:text-gray-900 ${
            action.className ?? ""
          }`}
          onClick={action.onClick}
          title={action.title}
          aria-label={action.label}
        >
          {action.icon}
        </button>
      )}
    </div>
  );
}
