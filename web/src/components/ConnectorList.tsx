import { useEffect, useRef, useState } from "react";
import { IconPlus, IconRefresh, IconX } from "@tabler/icons-react";
import {
  cancelConnectorOAuth,
  deleteInstallation,
  fetchCatalog,
  fetchInstallations,
  pollConnectorOAuth,
  startConnectorOAuth,
} from "../api/connectors";
import { useSessionStore } from "../stores/sessionStore";
import { Button } from "./ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "./ui/dialog";

type Flow =
  | { kind: "idle" }
  | { kind: "starting" }
  | { kind: "waiting"; loginId: string }
  | { kind: "error"; message: string };

/** The CONNECTORS sidebar section (connectors.md §4.1). Installations are
 * global; per-agent enablement lives in Agent settings. Installing is a popup
 * + status-poll OAuth flow — the callback lands on the server, and we poll
 * /oauth/status until it reports success. */
export function ConnectorList() {
  const token = useSessionStore((s) => s.token);
  const catalog = useSessionStore((s) => s.connectorCatalog);
  const setCatalog = useSessionStore((s) => s.setConnectorCatalog);
  const installations = useSessionStore((s) => s.connectorInstallations);
  const setInstallations = useSessionStore((s) => s.setConnectorInstallations);
  const removeInstallation = useSessionStore((s) => s.removeConnectorInstallation);

  const [open, setOpen] = useState(false);
  const [flow, setFlow] = useState<Flow>({ kind: "idle" });
  const polling = useRef(false);

  useEffect(() => {
    if (!token) return;
    fetchCatalog(token).then(setCatalog).catch(() => {});
    fetchInstallations(token).then(setInstallations).catch(() => {});
  }, [token, setCatalog, setInstallations]);

  // Stop any in-flight poll when the dialog closes.
  useEffect(() => {
    if (!open) polling.current = false;
  }, [open]);

  const connect = async (kind: string) => {
    setFlow({ kind: "starting" });
    try {
      const { login_id, authorize_url } = await startConnectorOAuth(token, kind);
      // Popup to the provider; the server handles the callback.
      window.open(authorize_url, "_blank", "noopener,noreferrer");
      setFlow({ kind: "waiting", loginId: login_id });
      polling.current = true;
      const deadline = Date.now() + 5 * 60 * 1000;
      while (polling.current && Date.now() < deadline) {
        await new Promise((r) => setTimeout(r, 1500));
        if (!polling.current) return;
        let st;
        try {
          st = await pollConnectorOAuth(token, login_id);
        } catch {
          continue; // transient; keep polling
        }
        if (st.status === "success") {
          setInstallations(await fetchInstallations(token));
          polling.current = false;
          setFlow({ kind: "idle" });
          setOpen(false);
          return;
        }
        if (st.status === "error" || st.status === "cancelled") {
          polling.current = false;
          setFlow({ kind: "error", message: st.message || "Sign-in failed." });
          return;
        }
      }
      if (polling.current) {
        polling.current = false;
        setFlow({ kind: "error", message: "Timed out waiting for sign-in." });
      }
    } catch (e) {
      setFlow({ kind: "error", message: (e as Error).message });
    }
  };

  const handleOpenChange = (v: boolean) => {
    if (!v && flow.kind === "waiting") {
      cancelConnectorOAuth(token, flow.loginId);
    }
    if (!v) setFlow({ kind: "idle" });
    setOpen(v);
  };

  const disconnect = async (id: string) => {
    try {
      await deleteInstallation(token, id);
      removeInstallation(id);
    } catch {
      // leave the row; a refetch will reconcile
    }
  };

  return (
    <div className="connector-section shrink-0">
      <div className="connector-header group flex h-8 items-center justify-between rounded-lg px-2 hover:bg-sidebar-accent transition-colors">
        <span className="text-[13px] font-medium leading-4 text-sidebar-foreground/50 group-hover:text-sidebar-foreground transition-colors uppercase tracking-wide">
          Connectors
        </span>
        <button
          className="btn-connector-add inline-flex h-6 w-6 items-center justify-center rounded-md text-sidebar-foreground/70 hover:bg-[hsl(var(--gray-200))] hover:text-sidebar-foreground transition-colors"
          onClick={() => {
            setFlow({ kind: "idle" });
            setOpen(true);
          }}
          title="Add connector"
          aria-label="Add connector"
        >
          <IconPlus size={14} />
        </button>
      </div>

      <div className="connector-list flex flex-col gap-0.5 mt-1">
        {installations.map((inst) => (
          <div
            key={inst.id}
            className="connector-item group flex items-center gap-2 rounded-lg px-2 py-1.5 text-sm text-sidebar-foreground hover:bg-sidebar-accent transition-colors"
          >
            <span className="connector-kind text-[10px] font-medium uppercase tracking-wider px-1.5 py-0.5 rounded shrink-0 bg-secondary text-secondary-foreground">
              {inst.kind}
            </span>
            <span className="connector-label truncate flex-1">{inst.label}</span>
            {inst.needs_reconnect && (
              <button
                className="btn-connector-reconnect inline-flex items-center gap-1 text-[10px] font-medium uppercase tracking-wider text-destructive shrink-0 hover:underline"
                onClick={() => {
                  setOpen(true);
                  connect(inst.kind);
                }}
                title="Reconnect"
              >
                <IconRefresh size={12} /> reconnect
              </button>
            )}
            <button
              className="btn-connector-delete inline-flex h-6 w-6 items-center justify-center rounded-md text-sidebar-foreground/60 opacity-0 group-hover:opacity-100 transition-opacity hover:bg-destructive/10 hover:text-destructive"
              onClick={() => disconnect(inst.id)}
              title="Disconnect"
            >
              <IconX size={14} />
            </button>
          </div>
        ))}
      </div>

      <Dialog open={open} onOpenChange={handleOpenChange}>
        <DialogContent className="connector-catalog">
          <DialogHeader>
            <DialogTitle>Add a connector</DialogTitle>
            <DialogDescription>
              Connect a third-party account once; then enable it per agent in
              Agent settings. Octopus stores the token encrypted at rest.
            </DialogDescription>
          </DialogHeader>

          {flow.kind === "waiting" && (
            <p className="text-sm text-muted-foreground">
              Waiting for sign-in in the opened tab… you can close this once
              it's done.
            </p>
          )}
          {flow.kind === "starting" && (
            <p className="text-sm text-muted-foreground">Starting sign-in…</p>
          )}
          {flow.kind === "error" && (
            <p className="text-sm text-destructive">{flow.message}</p>
          )}

          <div className="flex flex-col gap-2">
            {catalog.length === 0 && (
              <p className="text-sm text-muted-foreground">
                No connector kinds registered.
              </p>
            )}
            {catalog.map((c) => (
              <div
                key={c.kind}
                className="connector-catalog-item flex items-center gap-3 rounded-lg border-[0.7px] border-border px-3 py-2"
              >
                <div className="flex-1 min-w-0">
                  <div className="text-sm font-medium text-foreground">
                    {c.display_name}
                  </div>
                  {!c.available && (
                    <div className="text-xs text-muted-foreground">
                      Set its OAuth client id/secret in env to enable.
                    </div>
                  )}
                </div>
                <Button
                  size="sm"
                  className="btn-connector-connect"
                  disabled={!c.available || flow.kind === "waiting" || flow.kind === "starting"}
                  onClick={() => connect(c.kind)}
                >
                  Connect
                </Button>
              </div>
            ))}
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
