import { useCallback, useEffect } from "react";
import { IconPlus, IconTrash } from "@tabler/icons-react";
import { deleteApplication, fetchApplications } from "../api/applications";
import { useSessionStore, type Application } from "../stores/sessionStore";

/** Sidebar "Applications" section (applications.md §7) — the agent-built web
 * apps this Octopus owns. Clicking one renders it in the main pane like a
 * browser tab; the + opens the create form there. The list is seeded once
 * here and kept live by the `application_*` WS events. */
export function ApplicationList() {
  const token = useSessionStore((s) => s.token);
  const applications = useSessionStore((s) => s.applications);
  const setApplications = useSessionStore((s) => s.setApplications);
  const removeApplication = useSessionStore((s) => s.removeApplication);
  const activeApplicationId = useSessionStore((s) => s.activeApplicationId);
  const mainView = useSessionStore((s) => s.mainView);
  const openApplication = useSessionStore((s) => s.openApplication);
  const openApplicationCreate = useSessionStore((s) => s.openApplicationCreate);

  const load = useCallback(async () => {
    try {
      setApplications(await fetchApplications(token));
    } catch {
      // The sidebar is not the place to shout about a failed poll; the
      // create/open paths surface their own errors.
    }
  }, [token, setApplications]);

  useEffect(() => {
    if (token) load();
  }, [token, load]);

  const remove = async (app: Application) => {
    if (
      !window.confirm(
        `Delete "${app.name}"? This removes the application and its files.`
      )
    )
      return;
    // Optimistic: the WS `application_deleted` event would do this too, but
    // the click should feel instant.
    removeApplication(app.id);
    try {
      await deleteApplication(token, app.id);
    } catch {
      load();
    }
  };

  return (
    <div className="application-section shrink-0">
      <div className="application-header group flex h-8 items-center justify-between rounded-lg px-2 hover:bg-sidebar-accent transition-colors">
        <h2 className="text-[13px] font-medium leading-4 text-sidebar-foreground/50 group-hover:text-sidebar-foreground transition-colors uppercase tracking-wide">
          Applications
        </h2>
        <button
          className="btn-application-add inline-flex h-6 w-6 items-center justify-center rounded-md text-sidebar-foreground/70 hover:bg-[hsl(var(--gray-200))] hover:text-sidebar-foreground transition-colors"
          onClick={openApplicationCreate}
          title="New application"
          aria-label="New application"
        >
          <IconPlus size={14} />
        </button>
      </div>

      <div className="application-items flex flex-col gap-0 mt-1">
        {applications.map((app) => {
          const isActive =
            mainView === "application" && app.id === activeApplicationId;
          return (
            <div
              key={app.id}
              className={`application-item group flex items-center gap-2 rounded-lg px-2 py-1.5 cursor-pointer transition-colors ${
                isActive
                  ? "active bg-[hsl(var(--gray-200))] text-foreground"
                  : "text-sidebar-foreground hover:bg-sidebar-accent"
              }`}
              onClick={() => openApplication(app.id)}
              title={app.description || app.name}
            >
              <span
                className={`app-status-dot app-status-${app.status} inline-block size-2 rounded-full shrink-0 ${
                  app.status === "building"
                    ? "bg-primary animate-pulse"
                    : app.status === "failed"
                    ? "bg-destructive"
                    : "bg-muted-foreground/40"
                }`}
                aria-label={app.status}
              />
              <span className="application-icon shrink-0 text-base leading-none w-5 text-center">
                {app.icon || "🪟"}
              </span>
              <span
                className={`application-name truncate text-sm flex-1 ${
                  isActive ? "font-medium" : ""
                }`}
              >
                {app.name}
              </span>
              <div className="application-item-actions flex items-center gap-0.5 opacity-0 group-hover:opacity-100 transition-opacity">
                <button
                  className="btn-application-delete inline-flex h-6 w-6 items-center justify-center rounded-md text-sidebar-foreground/60 hover:bg-card hover:text-destructive"
                  onClick={(e) => {
                    e.stopPropagation();
                    remove(app);
                  }}
                  title="Delete application"
                  aria-label={`Delete ${app.name}`}
                >
                  <IconTrash size={13} />
                </button>
              </div>
            </div>
          );
        })}
        {applications.length === 0 && (
          <button
            type="button"
            className="application-empty text-left px-2 py-1.5 text-xs text-sidebar-foreground/50 hover:text-sidebar-foreground transition-colors"
            onClick={openApplicationCreate}
          >
            No applications yet — build one.
          </button>
        )}
      </div>
    </div>
  );
}
