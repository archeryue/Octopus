import type { ReactNode } from "react";
import { IconMenu2 } from "@tabler/icons-react";

/** The 60px bar every main-area view opens with.
 *
 * One shape across chat, the manage pages and the create forms: a breadcrumb
 * on the left (context → where you are) and actions or status pills flush
 * right. Keeping it identical everywhere is what makes the manage pages feel
 * like parts of the console rather than dialogs that lost their chrome.
 *
 * `crumbs` renders as `a / b / c` with only the last one emphasised — the
 * design sets the trailing crumb at 600 and the rest in muted grey.
 */
export function PageHeader({
  icon,
  crumbs,
  meta,
  actions,
  onToggleSidebar,
  className = "",
}: {
  icon?: ReactNode;
  crumbs: ReactNode[];
  /** Small muted text after the last crumb — counts, a summary, a chip. */
  meta?: ReactNode;
  actions?: ReactNode;
  onToggleSidebar?: () => void;
  /** Extra class for the bar — views tag it (`chat-header`, …) so tests and
   * view-specific CSS can address their own header. */
  className?: string;
}) {
  return (
    <div
      className={`page-header flex h-[60px] shrink-0 items-center gap-2.5 border-b border-gray-200 px-6 ${className}`}
    >
      {onToggleSidebar && (
        <button
          className="btn btn-menu -ml-2 inline-flex size-9 items-center justify-center rounded-lg text-gray-900 hover:bg-gray-100 md:hidden"
          onClick={onToggleSidebar}
          aria-label="Toggle sidebar"
        >
          <IconMenu2 size={18} />
        </button>
      )}
      {icon}
      {crumbs.map((crumb, i) => {
        const last = i === crumbs.length - 1;
        return (
          <span key={i} className="flex min-w-0 items-center gap-2.5">
            {i > 0 && (
              <span className="shrink-0 text-gray-500" aria-hidden>
                /
              </span>
            )}
            <span
              className={`truncate text-[14.5px] ${
                last ? "crumb-current font-semibold text-gray-950" : "text-gray-700"
              }`}
            >
              {crumb}
            </span>
          </span>
        );
      })}
      {meta && (
        <span className="page-header-meta shrink-0 font-mono text-[11.5px] text-gray-700">
          {meta}
        </span>
      )}
      {actions && (
        <div className="ml-auto flex shrink-0 items-center gap-2.5">{actions}</div>
      )}
    </div>
  );
}
