import { IconChevronLeft, IconChevronRight } from "@tabler/icons-react";
import { useSessionStore } from "../stores/sessionStore";

/** The hover-revealed handle that folds the sidebar down to its icon rail.
 *
 * It sits on the sidebar's edge rather than in the header because the edge is
 * where the pointer already is when you're thinking about the sidebar's width;
 * a header button is a fixed target you have to go and aim for. It stays
 * hidden until that edge is hovered (or the button is focused) so the resting
 * chrome is unchanged.
 *
 * The hover strip is kept *inside* the sidebar's own width so it can never
 * swallow a click meant for the main pane; only the button straddles the
 * border, and hovering it keeps the strip hovered because it's a descendant.
 */
export function SidebarEdgeToggle() {
  const collapsed = useSessionStore((s) => s.sidebarCollapsed);
  const setCollapsed = useSessionStore((s) => s.setSidebarCollapsed);
  const label = collapsed ? "Expand sidebar" : "Collapse sidebar";

  return (
    <div className="sidebar-edge">
      <button
        type="button"
        className="btn-sidebar-toggle"
        onClick={() => setCollapsed(!collapsed)}
        title={label}
        aria-label={label}
        aria-expanded={!collapsed}
      >
        {collapsed ? (
          <IconChevronRight size={13} />
        ) : (
          <IconChevronLeft size={13} />
        )}
      </button>
    </div>
  );
}
