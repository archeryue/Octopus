import { useCallback, useEffect } from "react";
import { IconPlus, IconTrash } from "@tabler/icons-react";
import { deleteApplication, fetchApplications } from "../api/applications";
import { useSessionStore } from "../stores/sessionStore";
import { SidebarSectionHeader } from "./SidebarSectionHeader";
import { AppIcon } from "./AppIcon";

/** The APPLICATIONS section — agent-built web apps, one row each.
 *
 * Rows are quieter than agent rows (no fold, no badge): an icon tile and a
 * name. Selecting one takes over the main pane with the app itself, so the
 * selected state matches the session rows above — tinted pill, accent border.
 * The list is seeded once here and kept live by the `application_*` WS events.
 */
export function SidebarApplications() {
  const token = useSessionStore((s) => s.token);
  const applications = useSessionStore((s) => s.applications);
  const setApplications = useSessionStore((s) => s.setApplications);
  const activeApplicationId = useSessionStore((s) => s.activeApplicationId);
  const mainView = useSessionStore((s) => s.mainView);
  const openApplication = useSessionStore((s) => s.openApplication);
  const openApplicationCreate = useSessionStore((s) => s.openApplicationCreate);
  const removeApplication = useSessionStore((s) => s.removeApplication);

  const load = useCallback(async () => {
    try {
      setApplications(await fetchApplications(token));
    } catch {
      // The sidebar isn't the place to shout about a failed poll.
    }
  }, [token, setApplications]);

  useEffect(() => {
    if (token) load();
  }, [token, load]);

  // Deleting is the permanent one — archiving (from the app's own header)
  // keeps the files. Both live where you'd reach for them: archive while
  // you're looking at the app, delete from the list you're pruning.
  const remove = async (id: string, name: string) => {
    if (
      !window.confirm(
        `Delete "${name}"? This removes the application and its files. ` +
          `Archive it instead to keep them.`
      )
    )
      return;
    removeApplication(id);
    try {
      await deleteApplication(token, id);
    } catch {
      load();
    }
  };

  return (
    <div className="application-section shrink-0">
      <SidebarSectionHeader
        label="Applications"
        className="application-header"
        action={{
          icon: <IconPlus size={14} />,
          onClick: openApplicationCreate,
          title: "New application",
          label: "New application",
          className: "btn-application-add",
        }}
      />

      <div className="application-items flex flex-col gap-0.5">
        {applications.map((app) => {
          const isActive =
            mainView === "application" && app.id === activeApplicationId;
          return (
            <div
              key={app.id}
              className={`application-item group/app flex cursor-pointer items-center gap-2.5 rounded-lg px-2.5 py-[7px] transition-colors ${
                isActive
                  ? "active bg-primary-50 border border-primary-100 shadow-[0_1px_2px_rgba(37,99,184,0.06)]"
                  : "border border-transparent hover:bg-gray-100"
              }`}
              onClick={() => openApplication(app.id)}
              title={
                app.description ? `${app.name} — ${app.description}` : app.name
              }
            >
              <AppIcon app={app} className="application-icon shrink-0" />
              <span
                className={`application-name truncate text-[13.5px] ${
                  isActive
                    ? "font-semibold text-gray-950"
                    : "font-medium text-gray-800"
                }`}
              >
                {app.name}
              </span>
              {app.status !== "ready" && (
                <span
                  className={`app-status-dot app-status-${app.status} ml-auto inline-block size-[7px] shrink-0 rounded-full ${
                    app.status === "building"
                      ? "bg-primary animate-pulse"
                      : "bg-warn"
                  }`}
                  aria-label={app.status}
                />
              )}
              <button
                className={`btn-application-delete inline-flex size-5 shrink-0 items-center justify-center rounded-md text-gray-600 opacity-0 transition-opacity hover:bg-danger-bg hover:text-destructive group-hover/app:opacity-100 ${
                  app.status === "ready" ? "ml-auto" : ""
                }`}
                onClick={(e) => {
                  e.stopPropagation();
                  remove(app.id, app.name);
                }}
                title="Delete application"
                aria-label={`Delete ${app.name}`}
              >
                <IconTrash size={13} />
              </button>
            </div>
          );
        })}
        {applications.length === 0 && (
          <button
            type="button"
            className="application-empty px-2.5 py-1.5 text-left text-[12.5px] text-gray-700 transition-colors hover:text-gray-900"
            onClick={openApplicationCreate}
          >
            No applications yet — build one.
          </button>
        )}
      </div>
    </div>
  );
}
