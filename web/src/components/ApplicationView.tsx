import { useEffect, useMemo, useRef, useState } from "react";
import {
  IconAlertTriangle,
  IconArchive,
  IconArrowUp,
  IconExternalLink,
  IconMessage,
  IconRefresh,
} from "@tabler/icons-react";
import {
  applicationUrl,
  archiveApplication,
  buildApplication,
  primeAppCookie,
} from "../api/applications";
import { selectSession } from "../lib/selectSession";
import { useSessionStore } from "../stores/sessionStore";
import { PageHeader } from "./PageHeader";
import { Button } from "./ui/button";

/** The main pane for one application (applications.md §7) — the browser-tab
 * view. Renders the app's own document in an iframe once it's `ready`, shows
 * the build in progress before that, and carries a composer that turns a
 * sentence into another build turn in the same session. */
export function ApplicationView({
  onToggleSidebar,
}: {
  onToggleSidebar: () => void;
}) {
  const token = useSessionStore((s) => s.token);
  const applications = useSessionStore((s) => s.applications);
  const activeApplicationId = useSessionStore((s) => s.activeApplicationId);
  const agents = useSessionStore((s) => s.agents);
  const upsertApplication = useSessionStore((s) => s.upsertApplication);
  const removeApplication = useSessionStore((s) => s.removeApplication);

  const app = applications.find((a) => a.id === activeApplicationId) ?? null;
  const agent = agents.find((a) => a.id === app?.agent_id) ?? null;

  const [request, setRequest] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Bumped to force the iframe to re-navigate (a same-src assignment is a
  // no-op). Also bumped on every fresh `last_built_at`, so a finished rebuild
  // swaps in without the user reaching for reload.
  const [nonce, setNonce] = useState(0);
  const lastBuiltRef = useRef<string | null>(null);

  const status = app?.status;
  const lastBuiltAt = app?.last_built_at ?? null;

  useEffect(() => {
    if (!app) return;
    if (lastBuiltRef.current !== lastBuiltAt) {
      lastBuiltRef.current = lastBuiltAt;
      setNonce((n) => n + 1);
    }
  }, [app, lastBuiltAt]);

  // The iframe and everything it loads authenticate with this cookie — an
  // iframe can't carry an Authorization header (applications.md §3). This has
  // to happen BEFORE the frame element reaches the DOM, because the browser
  // starts its request the moment it does — earlier than any effect would run.
  // A memo is the one hook that runs during render; the write is idempotent,
  // so a render React throws away costs nothing.
  useMemo(() => {
    if (token) primeAppCookie(token);
  }, [token]);

  // A pending request is cleared once its turn actually starts.
  useEffect(() => {
    if (status === "building") setBusy(false);
  }, [status]);

  const src = useMemo(
    () => (app ? applicationUrl(app.id, nonce) : ""),
    [app, nonce]
  );

  if (!app) {
    return (
      <div className="application-view flex-1 flex items-center justify-center text-muted-foreground">
        <p className="text-sm">This application is no longer available.</p>
      </div>
    );
  }

  const archive = async () => {
    if (
      !window.confirm(
        `Archive "${app.name}"? It leaves the sidebar but keeps its files — ` +
          `restore it any time from the Archived tab.`
      )
    )
      return;
    try {
      await archiveApplication(token, app.id);
      removeApplication(app.id);
    } catch {
      // The WS event would have done this too; leave the view as-is.
    }
  };

  const openBuildSession = () => {
    if (app.session_id) selectSession(app.session_id, app.agent_id);
  };

  const askForChanges = async () => {
    const prompt = request.trim();
    if (!prompt || busy) return;
    setBusy(true);
    setError(null);
    try {
      upsertApplication(await buildApplication(token, app.id, prompt));
      setRequest("");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to start the build");
    } finally {
      setBusy(false);
    }
  };

  const statusBadge = (
    <span
      className={`app-status-badge app-status-${app.status} pill ${
        app.status === "building"
          ? "pill-neutral text-primary"
          : app.status === "failed"
          ? "pill-warn"
          : "pill-success"
      }`}
    >
      <span
        className={`dot ${app.status === "building" ? "animate-pulse" : ""}`}
      />
      {app.status === "building"
        ? "Building"
        : app.status === "failed"
        ? "Build failed"
        : `Built by ${agent?.name ?? "an agent"}`}
    </span>
  );

  return (
    <div className="application-view flex-1 flex flex-col min-h-0">
      <PageHeader
        onToggleSidebar={onToggleSidebar}
        icon={
          <span className="tile tile-blue shrink-0" aria-hidden>
            {app.icon || "🪟"}
          </span>
        }
        crumbs={["Applications", <span key="name" className="application-title">{app.name}</span>]}
        meta={statusBadge}
        actions={
          <>
            <button
              className="btn-application-reload inline-flex size-8 items-center justify-center rounded-lg text-gray-700 transition-colors hover:bg-gray-100 hover:text-gray-950"
              onClick={() => setNonce((n) => n + 1)}
              title="Reload the app"
              aria-label="Reload the app"
            >
              <IconRefresh size={16} />
            </button>
            <a
              className="btn-application-open-tab inline-flex size-8 items-center justify-center rounded-lg text-gray-700 transition-colors hover:bg-gray-100 hover:text-gray-950"
              href={applicationUrl(app.id)}
              target="_blank"
              rel="noreferrer"
              title="Open in a new tab"
              aria-label="Open in a new tab"
            >
              <IconExternalLink size={16} />
            </a>
            <button
              className="btn-application-archive inline-flex size-8 items-center justify-center rounded-lg text-gray-700 transition-colors hover:bg-warn-bg hover:text-warn-foreground"
              onClick={archive}
              title="Archive this application"
              aria-label="Archive this application"
            >
              <IconArchive size={16} />
            </button>
            {app.session_id && (
              <Button
                variant="outline"
                size="sm"
                className="btn-application-open-session"
                onClick={openBuildSession}
                title={`Open the build session${agent ? ` with ${agent.name}` : ""}`}
              >
                <IconMessage size={15} />
                <span className="hidden sm:inline">Build session</span>
              </Button>
            )}
          </>
        }
      />

      <div className="application-frame-wrap flex-1 min-h-0 bg-background relative">
        {app.status === "ready" ? (
          <iframe
            key={src}
            className="application-frame w-full h-full border-0 bg-white"
            src={src}
            title={app.name}
            // `allow-same-origin` is deliberate: without it the app gets an
            // opaque origin and localStorage throws, which breaks most small
            // web apps. `allow-top-navigation` is withheld, so a buggy app
            // can't navigate the Octopus tab away (applications.md §3).
            sandbox="allow-scripts allow-same-origin allow-forms allow-popups allow-modals allow-downloads"
          />
        ) : app.status === "building" ? (
          <div className="application-building absolute inset-0 flex flex-col items-center justify-center gap-3 text-center px-6">
            <span className="inline-block size-6 rounded-full border-2 border-primary/30 border-t-primary animate-spin" />
            <p className="text-sm font-medium text-foreground">
              {agent ? `${agent.name} is building ${app.name}…` : "Building…"}
            </p>
            <p className="text-xs text-muted-foreground max-w-sm leading-relaxed">
              It'll appear here the moment the entry page lands. You can watch
              the work in the build session.
            </p>
            {app.session_id && (
              <Button
                variant="outline"
                size="sm"
                className="btn-application-watch"
                onClick={openBuildSession}
              >
                <IconMessage size={15} />
                Watch the build
              </Button>
            )}
          </div>
        ) : (
          <div className="application-failed absolute inset-0 flex flex-col items-center justify-center gap-3 text-center px-6">
            <IconAlertTriangle size={22} className="text-destructive" />
            <p className="text-sm font-medium text-foreground">
              This build didn't produce a page yet
            </p>
            <p className="application-error text-xs text-muted-foreground max-w-md leading-relaxed">
              {app.error || `${app.entrypoint} is missing.`}
            </p>
            <p className="text-xs text-muted-foreground">
              Ask for a fix below — it runs in the same session.
            </p>
          </div>
        )}
      </div>

      <div className="application-compose shrink-0 border-t border-border bg-background px-4 py-2">
        {error && (
          <div className="application-compose-error mb-2 rounded-lg border-[0.7px] border-destructive/40 bg-destructive/10 px-3 py-1.5 text-xs text-destructive">
            {error}
          </div>
        )}
        <div className="flex items-end gap-2">
          <textarea
            className="application-request-input flex-1 min-h-9 max-h-32 resize-none rounded-xl border-[0.7px] border-gray-400 bg-card px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground outline-none transition-colors focus:border-primary/70 focus:ring-[3px] focus:ring-primary/10"
            value={request}
            onChange={(e) => setRequest(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                askForChanges();
              }
            }}
            rows={1}
            placeholder={`Ask ${agent?.name || "the agent"} for a change — "add a dark mode toggle"`}
          />
          <Button
            className="btn-application-request"
            size="icon"
            onClick={askForChanges}
            disabled={busy || !request.trim()}
            title="Send the change request"
            aria-label="Send the change request"
          >
            <IconArrowUp size={18} />
          </Button>
        </div>
      </div>
    </div>
  );
}
