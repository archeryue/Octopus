import { useCallback, useEffect } from "react";
import { IconActivity, IconBolt, IconClock, IconSettings } from "@tabler/icons-react";
import { useSessionStore, type CredentialInfo, type Schedule } from "../stores/sessionStore";
import { SidebarSectionHeader } from "./SidebarSectionHeader";

const API = window.location.origin;

/** Format the next fire as the design writes it — "next 09:00". Anything
 * further out than today says which day, because "next 09:00" on a Monday
 * schedule read on Friday is a lie. */
function nextRunLabel(schedules: Schedule[]): string | null {
  const upcoming = schedules
    .filter((s) => s.enabled && s.next_run_at)
    .map((s) => new Date(s.next_run_at as string))
    .filter((d) => !Number.isNaN(d.getTime()))
    .sort((a, b) => a.getTime() - b.getTime());
  const next = upcoming[0];
  if (!next) return null;
  const time = next.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
  const sameDay = next.toDateString() === new Date().toDateString();
  return sameDay ? `next ${time}` : `next ${next.toLocaleDateString([], { weekday: "short" })} ${time}`;
}

/** The MANAGE group — the system half of the sidebar.
 *
 * Three summary rows, each a link to a full page in the main area (the
 * console design moved these out of dialogs). The right-hand summary is the
 * point: it carries the count plus a health dot, so a lapsed credential or a
 * broken connector is visible from anywhere in the app without opening
 * anything.
 */
export function SidebarManage() {
  const token = useSessionStore((s) => s.token);
  const mainView = useSessionStore((s) => s.mainView);
  const openManage = useSessionStore((s) => s.openManage);
  const schedules = useSessionStore((s) => s.schedules);
  const setSchedules = useSessionStore((s) => s.setSchedules);
  const credentials = useSessionStore((s) => s.credentials);
  const setCredentials = useSessionStore((s) => s.setCredentials);
  const installations = useSessionStore((s) => s.connectorInstallations);

  const load = useCallback(async () => {
    const headers = { Authorization: `Bearer ${token}` };
    try {
      const [sRes, cRes] = await Promise.all([
        fetch(`${API}/api/schedules`, { headers }),
        fetch(`${API}/api/credentials`, { headers }),
      ]);
      if (sRes.ok) setSchedules((await sRes.json()) as Schedule[]);
      if (cRes.ok) setCredentials((await cRes.json()) as CredentialInfo[]);
    } catch {
      // ignore
    }
  }, [token, setSchedules, setCredentials]);

  useEffect(() => {
    if (token) load();
  }, [token, load]);

  const connectorError = installations.some((i) => i.needs_reconnect);
  const credentialError = credentials.some(
    (c) => c.needs_reconnect
  );
  const next = nextRunLabel(schedules);

  return (
    <div className="manage-section shrink-0">
      <div className="mx-1.5 mt-3.5 border-t border-gray-300" />
      <SidebarSectionHeader label="Manage" className="manage-header pt-3" />

      <div className="manage-items flex flex-col gap-0.5">
        <ManageRow
          icon={<IconClock size={14} />}
          label="Schedules"
          className="btn-manage-schedules"
          active={mainView === "schedules"}
          onClick={() => openManage("schedules")}
          summary={
            <>
              {schedules.length}
              {next && <span className="text-gray-600"> · {next}</span>}
            </>
          }
        />
        <ManageRow
          icon={<IconBolt size={14} />}
          label="Connectors"
          className="btn-manage-connectors"
          active={mainView === "connectors"}
          onClick={() => openManage("connectors")}
          summary={
            <>
              {connectorError ? (
                <span className="text-warn-foreground">needs reconnect</span>
              ) : (
                installations.length
              )}
              <span
                className={`inline-block size-1.5 rounded-full ${
                  connectorError ? "bg-warn" : "bg-success"
                }`}
              />
            </>
          }
        />
        <ManageRow
          icon={<IconActivity size={14} />}
          label="Monitor"
          className="btn-manage-monitor"
          active={mainView === "monitor"}
          onClick={() => openManage("monitor")}
          summary={<span className="text-gray-600">30d</span>}
        />
        <ManageRow
          icon={<IconSettings size={14} />}
          label="Harness"
          className="btn-manage-harness"
          active={mainView === "harness"}
          onClick={() => openManage("harness")}
          summary={
            <>
              {credentialError ? (
                <span className="text-warn-foreground">needs reconnect</span>
              ) : (
                credentials.length
              )}
              <span
                className={`inline-block size-1.5 rounded-full ${
                  credentialError ? "bg-warn" : "bg-success"
                }`}
              />
            </>
          }
        />
      </div>
    </div>
  );
}

function ManageRow({
  icon,
  label,
  summary,
  active,
  onClick,
  className,
}: {
  icon: React.ReactNode;
  label: string;
  summary: React.ReactNode;
  active: boolean;
  onClick: () => void;
  className: string;
}) {
  return (
    <button
      type="button"
      className={`manage-item ${className} flex w-full items-center gap-2.5 rounded-lg px-2.5 py-[7px] text-left transition-colors ${
        active
          ? "active bg-primary-50 border border-primary-100 shadow-[0_1px_2px_rgba(37,99,184,0.06)]"
          : "border border-transparent hover:bg-gray-100"
      }`}
      onClick={onClick}
      /* The label is hidden when the sidebar is collapsed, so the icon needs
         to say what it is some other way. */
      title={label}
    >
      <span className="inline-flex w-5 shrink-0 justify-center text-gray-700">
        {icon}
      </span>
      <span
        className={`manage-label flex-1 truncate text-[13.5px] ${
          active ? "font-semibold text-gray-950" : "text-gray-800"
        }`}
      >
        {label}
      </span>
      <span className="manage-summary flex shrink-0 items-center gap-1.5 font-mono text-[10.5px] text-gray-700">
        {summary}
      </span>
    </button>
  );
}
