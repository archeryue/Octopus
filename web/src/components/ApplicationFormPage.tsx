import { useCallback, useEffect, useState } from "react";
import { IconArchive, IconSparkles } from "@tabler/icons-react";
import {
  createApplication,
  fetchApplications,
  unarchiveApplication,
} from "../api/applications";
import { useSessionStore, type Application } from "../stores/sessionStore";
import { PageHeader } from "./PageHeader";
import { Button } from "./ui/button";
import { Input } from "./ui/input";
import { Label } from "./ui/label";

type Tab = "archived" | "create";

/** The New Application page — a full-page form in the main area.
 *
 * Two tabs, matching the agent form: **Archived** lists applications you put
 * away, one card each, ready to restore (this is the design's "market" slot,
 * filled with your own shelved apps rather than a catalog); **Create**
 * describes a new one for an agent to build.
 */
export function ApplicationFormPage({
  onToggleSidebar,
}: {
  onToggleSidebar: () => void;
}) {
  const token = useSessionStore((s) => s.token);
  const agents = useSessionStore((s) => s.agents);
  const activeAgentId = useSessionStore((s) => s.activeAgentId);
  const upsertApplication = useSessionStore((s) => s.upsertApplication);
  const openApplication = useSessionStore((s) => s.openApplication);
  const showChat = useSessionStore((s) => s.showChat);

  const [tab, setTab] = useState<Tab>("create");
  const [archived, setArchived] = useState<Application[]>([]);
  const [name, setName] = useState("");
  const [icon, setIcon] = useState("");
  const [description, setDescription] = useState("");
  const [instructions, setInstructions] = useState("");
  const [pickedAgentId, setPickedAgentId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadArchived = useCallback(async () => {
    try {
      setArchived(await fetchApplications(token, { archived: true }));
    } catch {
      // The tab simply shows empty; the create flow is unaffected.
    }
  }, [token]);

  useEffect(() => {
    // Fetch-on-mount: the state lands in a promise callback, not in the
    // effect body, so there's no cascading render — the lint rule can't see
    // through the async boundary.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void loadArchived();
  }, [loadArchived]);

  // Derived, not synced through an effect, so a late-arriving agent list can't
  // stomp a choice the user already made.
  const defaultAgentId = (
    agents.find((a) => a.id === activeAgentId) ??
    agents.find((a) => a.is_system) ??
    agents[0]
  )?.id;
  const agentId =
    pickedAgentId && agents.some((a) => a.id === pickedAgentId)
      ? pickedAgentId
      : defaultAgentId ?? "";

  const canSubmit =
    !busy && name.trim().length > 0 && description.trim().length > 0 && !!agentId;

  const submit = async () => {
    if (!canSubmit) return;
    setBusy(true);
    setError(null);
    try {
      const app = await createApplication(token, {
        name: name.trim(),
        description: description.trim(),
        agent_id: agentId,
        icon: icon.trim() || null,
        instructions: instructions.trim(),
      });
      upsertApplication(app);
      openApplication(app.id);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to create application");
      setBusy(false);
    }
  };

  const restore = async (app: Application) => {
    try {
      const restored = await unarchiveApplication(token, app.id);
      upsertApplication(restored);
      setArchived((cur) => cur.filter((a) => a.id !== app.id));
      openApplication(restored.id);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to restore");
    }
  };

  return (
    <div className="application-create flex min-h-0 flex-1 flex-col">
      <PageHeader
        crumbs={["Applications", "New Application"]}
        onToggleSidebar={onToggleSidebar}
        actions={
          <>
            <TabSwitch tab={tab} setTab={setTab} archivedCount={archived.length} />
            <button
              type="button"
              className="btn-application-create-close text-[13.5px] text-gray-800 transition-colors hover:text-gray-950"
              onClick={showChat}
            >
              Cancel
            </button>
            {tab === "create" && (
              <Button
                className="btn-application-create"
                size="sm"
                onClick={submit}
                disabled={!canSubmit}
              >
                <IconSparkles size={15} />
                {busy ? "Starting…" : "Start Build"}
              </Button>
            )}
          </>
        }
      />

      <div className="page-body">
        <div className="mx-auto w-full max-w-4xl">
          {error && (
            <div className="app-create-error mb-4 rounded-lg border border-danger-border bg-danger-bg px-3.5 py-2.5 text-[13px] text-destructive">
              {error}
            </div>
          )}

          {tab === "archived" ? (
            <ArchivedGrid items={archived} onRestore={restore} />
          ) : (
            <div className="space-y-6">
              <div className="flex items-start gap-4">
                <span className="tile tile-blue mt-7 size-14 rounded-xl text-2xl">
                  {icon || "🪟"}
                </span>
                <div className="grid flex-1 gap-4 md:grid-cols-2">
                  <div className="space-y-2">
                    <Label htmlFor="app-name">Name</Label>
                    <Input
                      id="app-name"
                      className="app-name-input"
                      value={name}
                      onChange={(e) => setName(e.target.value)}
                      placeholder="Weekly Report Generator"
                      autoFocus
                    />
                  </div>
                  <div className="space-y-2">
                    <Label htmlFor="app-icon-goal">One-line goal</Label>
                    <div className="flex gap-2">
                      <Input
                        id="app-icon"
                        className="app-icon-input w-16 text-center"
                        value={icon}
                        onChange={(e) => setIcon(e.target.value)}
                        placeholder="🪟"
                        maxLength={4}
                        aria-label="Icon"
                      />
                      <Input
                        id="app-icon-goal"
                        className="app-goal-input flex-1"
                        value={instructions}
                        onChange={(e) => setInstructions(e.target.value)}
                        placeholder="Every Monday, a one-page shareable team weekly"
                      />
                    </div>
                  </div>
                </div>
              </div>

              <div className="space-y-2">
                <Label htmlFor="app-description">
                  What problem does this app solve?
                  <span className="ml-2 font-normal text-gray-700">
                    the agent builds from this description
                  </span>
                </Label>
                <textarea
                  id="app-description"
                  className="app-description-input min-h-40 w-full rounded-xl border border-gray-400 bg-card px-4 py-3 text-[13.5px] leading-relaxed text-gray-900 outline-none transition-colors placeholder:text-gray-600 focus:border-primary focus:ring-[3px] focus:ring-primary/10"
                  value={description}
                  onChange={(e) => setDescription(e.target.value)}
                  placeholder="Every Monday 09:00 auto-summarize each agent's completed tasks from last week into a one-page weekly: headline numbers on top, key outputs grouped by agent below, exportable as a share link. Mark gaps when data is incomplete instead of making things up."
                />
              </div>

              <div className="space-y-2">
                <Label>
                  Which agent builds it
                  <span className="ml-2 font-normal text-gray-700">
                    a new session is created under that agent
                  </span>
                </Label>
                <div className="grid gap-3 md:grid-cols-3">
                  {agents.map((a) => {
                    const picked = a.id === agentId;
                    return (
                      <button
                        key={a.id}
                        type="button"
                        className={`app-agent-option flex items-center gap-3 rounded-xl border px-4 py-3 text-left transition-colors ${
                          picked
                            ? "border-primary-200 bg-primary-50"
                            : "border-gray-400 hover:bg-gray-50"
                        }`}
                        onClick={() => setPickedAgentId(a.id)}
                        aria-pressed={picked}
                      >
                        <span className="tile tile-warm">{a.avatar || "🐙"}</span>
                        <span className="min-w-0 flex-1">
                          <span
                            className={`block truncate text-[13.5px] ${
                              picked ? "font-semibold text-primary" : "text-gray-900"
                            }`}
                          >
                            {a.name}
                          </span>
                          <span className="block truncate font-mono text-[10.5px] text-gray-700">
                            {a.backend}
                          </span>
                        </span>
                        {picked && (
                          <span className="size-2.5 shrink-0 rounded-full bg-primary" />
                        )}
                      </button>
                    );
                  })}
                </div>
                {/* A hidden native select keeps the form keyboard- and
                 * test-addressable without a second visible control. */}
                <select
                  id="app-agent"
                  className="sr-only"
                  value={agentId}
                  onChange={(e) => setPickedAgentId(e.target.value)}
                  aria-label="Which agent builds it"
                >
                  {agents.map((a) => (
                    <option key={a.id} value={a.id}>
                      {a.name}
                    </option>
                  ))}
                </select>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function TabSwitch({
  tab,
  setTab,
  archivedCount,
}: {
  tab: Tab;
  setTab: (t: Tab) => void;
  archivedCount: number;
}) {
  return (
    <div className="form-tabs flex items-center rounded-lg border border-gray-400 bg-gray-50 p-0.5">
      <button
        type="button"
        className={`btn-tab-archived rounded-md px-3 py-1 text-[13px] transition-colors ${
          tab === "archived"
            ? "bg-card font-semibold text-gray-950 shadow-sm"
            : "text-gray-800"
        }`}
        onClick={() => setTab("archived")}
      >
        Archived{archivedCount > 0 ? ` ${archivedCount}` : ""}
      </button>
      <button
        type="button"
        className={`btn-tab-create rounded-md px-3 py-1 text-[13px] transition-colors ${
          tab === "create"
            ? "bg-card font-semibold text-gray-950 shadow-sm"
            : "text-gray-800"
        }`}
        onClick={() => setTab("create")}
      >
        Create
      </button>
    </div>
  );
}

function ArchivedGrid({
  items,
  onRestore,
}: {
  items: Application[];
  onRestore: (app: Application) => void;
}) {
  if (items.length === 0) {
    return (
      <div className="archived-empty card px-6 py-12 text-center">
        <IconArchive size={22} className="mx-auto mb-3 text-gray-600" />
        <p className="text-sm text-gray-900">Nothing archived.</p>
        <p className="mt-1.5 text-[13px] text-gray-700">
          Archiving an application keeps its files, so restoring puts it back
          exactly as it was.
        </p>
      </div>
    );
  }
  return (
    <div className="archived-grid grid gap-4 md:grid-cols-2 xl:grid-cols-3">
      {items.map((app) => (
        <div key={app.id} className="archived-item card flex flex-col px-5 py-4">
          <div className="flex items-center gap-2.5">
            <span className="tile tile-lg tile-blue">{app.icon || "🪟"}</span>
            <span className="truncate text-[15px] font-semibold text-gray-950">
              {app.name}
            </span>
          </div>
          <p className="mt-3 line-clamp-4 flex-1 text-[13px] leading-relaxed text-gray-800">
            {app.description}
          </p>
          <div className="mt-4 flex items-center gap-2">
            <span className="font-mono text-[10.5px] text-gray-700">
              {app.status}
            </span>
            <Button
              size="sm"
              variant="outline"
              className="btn-restore ml-auto"
              onClick={() => onRestore(app)}
            >
              Restore
            </Button>
          </div>
        </div>
      ))}
    </div>
  );
}
