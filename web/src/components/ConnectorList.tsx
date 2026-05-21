import { useEffect, useRef, useState } from "react";
import { IconArrowLeft, IconPlus, IconRefresh, IconX } from "@tabler/icons-react";
import {
  cancelConnectorOAuth,
  createCustomConnector,
  deleteCustomConnector,
  deleteInstallation,
  fetchCatalog,
  fetchInstallations,
  getOAuthClient,
  pollConnectorOAuth,
  setOAuthClient,
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
import { Input } from "./ui/input";
import { Label } from "./ui/label";

type Flow =
  | { kind: "idle" }
  | { kind: "starting" }
  | { kind: "waiting"; loginId: string }
  | { kind: "error"; message: string };

type View = "catalog" | "setup" | "custom";

const REDIRECT_PATH = "/api/connectors/oauth/callback";

/** The CONNECTORS sidebar section (connectors.md). Everything is browser-only:
 * install (OAuth popup), configure a built-in connector's OAuth client, and
 * define a brand-new custom connector — no server access needed. */
export function ConnectorList() {
  const token = useSessionStore((s) => s.token);
  const catalog = useSessionStore((s) => s.connectorCatalog);
  const setCatalog = useSessionStore((s) => s.setConnectorCatalog);
  const installations = useSessionStore((s) => s.connectorInstallations);
  const setInstallations = useSessionStore((s) => s.setConnectorInstallations);
  const removeInstallation = useSessionStore((s) => s.removeConnectorInstallation);

  const [open, setOpen] = useState(false);
  const [view, setView] = useState<View>("catalog");
  const [flow, setFlow] = useState<Flow>({ kind: "idle" });
  const polling = useRef(false);

  // Setup-a-built-in form.
  const [setupKind, setSetupKind] = useState("");
  const [setupRedirect, setSetupRedirect] = useState("");
  const [clientId, setClientId] = useState("");
  const [clientSecret, setClientSecret] = useState("");
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  // Add-custom-connector form.
  const blankCustom = {
    kind: "",
    display_name: "",
    authorize_url: "",
    token_url: "",
    scopes: "",
    pkce: false,
    api_base: "",
    client_id: "",
    client_secret: "",
  };
  const [custom, setCustom] = useState({ ...blankCustom });

  // The redirect URI to register with a provider — what the browser is hitting.
  const browserRedirect = `${window.location.origin}${REDIRECT_PATH}`;
  const setupEntry = catalog.find((c) => c.kind === setupKind);

  const refreshCatalog = () => fetchCatalog(token).then(setCatalog).catch(() => {});

  useEffect(() => {
    if (!token) return;
    refreshCatalog();
    fetchInstallations(token).then(setInstallations).catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token]);

  useEffect(() => {
    if (!open) polling.current = false;
  }, [open]);

  const resetDialog = () => {
    setView("catalog");
    setFlow({ kind: "idle" });
    setFormError(null);
    setClientId("");
    setClientSecret("");
    setCustom({ ...blankCustom });
  };

  // --- OAuth install (popup + poll) ---------------------------------------

  const connect = async (kind: string) => {
    setFlow({ kind: "starting" });
    try {
      const { login_id, authorize_url } = await startConnectorOAuth(token, kind);
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
          continue;
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

  // --- configure a built-in connector's OAuth client ----------------------

  const openSetup = async (kind: string) => {
    setSetupKind(kind);
    setClientId("");
    setClientSecret("");
    setFormError(null);
    setSetupRedirect(browserRedirect);
    setView("setup");
    try {
      const cfg = await getOAuthClient(token, kind);
      setSetupRedirect(cfg.redirect_uri || browserRedirect);
      if (cfg.client_id) setClientId(cfg.client_id);
    } catch {
      // keep the browser-derived redirect
    }
  };

  const saveSetup = async () => {
    setBusy(true);
    setFormError(null);
    try {
      await setOAuthClient(token, setupKind, clientId.trim(), clientSecret.trim());
      await refreshCatalog();
      resetDialog();
    } catch (e) {
      setFormError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  // --- add a custom connector ---------------------------------------------

  const saveCustom = async () => {
    setBusy(true);
    setFormError(null);
    try {
      await createCustomConnector(token, {
        kind: custom.kind.trim(),
        display_name: custom.display_name.trim() || custom.kind.trim(),
        authorize_url: custom.authorize_url.trim(),
        token_url: custom.token_url.trim(),
        scopes: custom.scopes.split(/[\s,]+/).filter(Boolean),
        pkce: custom.pkce,
        api_base: custom.api_base.trim(),
        client_id: custom.client_id.trim(),
        client_secret: custom.client_secret.trim(),
      });
      await refreshCatalog();
      resetDialog();
    } catch (e) {
      setFormError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const removeCustom = async (kind: string) => {
    try {
      await deleteCustomConnector(token, kind);
      await refreshCatalog();
    } catch {
      // ignore; a refetch reconciles
    }
  };

  const handleOpenChange = (v: boolean) => {
    if (!v && flow.kind === "waiting") cancelConnectorOAuth(token, flow.loginId);
    if (!v) resetDialog();
    setOpen(v);
  };

  const disconnect = async (id: string) => {
    try {
      await deleteInstallation(token, id);
      removeInstallation(id);
    } catch {
      /* leave the row; a refetch reconciles */
    }
  };

  const cu = (k: keyof typeof custom, v: string | boolean) =>
    setCustom((c) => ({ ...c, [k]: v }));

  const redirectBox = (uri: string) => (
    <div className="space-y-1">
      <Label>Redirect URI (register this with the provider)</Label>
      <div className="flex items-center gap-2">
        <code className="flex-1 break-all rounded-md border-[0.7px] border-border bg-muted/40 px-2 py-1.5 text-xs font-mono text-foreground">
          {uri}
        </code>
        <Button
          type="button"
          variant="secondary"
          size="sm"
          className="btn-copy-redirect shrink-0"
          onClick={() => navigator.clipboard?.writeText(uri).catch(() => {})}
        >
          Copy
        </Button>
      </div>
    </div>
  );

  return (
    <div className="connector-section shrink-0">
      <div className="connector-header group flex h-8 items-center justify-between rounded-lg px-2 hover:bg-sidebar-accent transition-colors">
        <span className="text-[13px] font-medium leading-4 text-sidebar-foreground/50 group-hover:text-sidebar-foreground transition-colors uppercase tracking-wide">
          Connectors
        </span>
        <button
          className="btn-connector-add inline-flex h-6 w-6 items-center justify-center rounded-md text-sidebar-foreground/70 hover:bg-[hsl(var(--gray-200))] hover:text-sidebar-foreground transition-colors"
          onClick={() => {
            resetDialog();
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
                  resetDialog();
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
          {view === "catalog" && (
            <>
              <DialogHeader>
                <DialogTitle>Connectors</DialogTitle>
                <DialogDescription>
                  Connect a third-party account once, then enable it per agent
                  in Agent settings. Tokens are stored encrypted at rest.
                </DialogDescription>
              </DialogHeader>

              {flow.kind === "waiting" && (
                <p className="text-sm text-muted-foreground">
                  Waiting for sign-in in the opened tab…
                </p>
              )}
              {flow.kind === "starting" && (
                <p className="text-sm text-muted-foreground">Starting sign-in…</p>
              )}
              {flow.kind === "error" && (
                <p className="text-sm text-destructive">{flow.message}</p>
              )}

              <div className="flex flex-col gap-2">
                {catalog.map((c) => (
                  <div
                    key={c.kind}
                    className="connector-catalog-item flex items-center gap-3 rounded-lg border-[0.7px] border-border px-3 py-2"
                  >
                    <div className="flex-1 min-w-0">
                      <div className="text-sm font-medium text-foreground">
                        {c.display_name}
                        {c.custom && (
                          <span className="ml-2 text-[10px] uppercase tracking-wider text-muted-foreground">
                            custom
                          </span>
                        )}
                      </div>
                      {!c.available && (
                        <div className="text-xs text-muted-foreground">
                          Needs an OAuth client — set it up.
                        </div>
                      )}
                    </div>
                    {c.custom && (
                      <button
                        className="btn-connector-remove text-muted-foreground hover:text-destructive"
                        title="Remove connector"
                        onClick={() => removeCustom(c.kind)}
                      >
                        <IconX size={15} />
                      </button>
                    )}
                    {c.available ? (
                      <Button
                        size="sm"
                        className="btn-connector-connect"
                        disabled={flow.kind === "waiting" || flow.kind === "starting"}
                        onClick={() => connect(c.kind)}
                      >
                        Connect
                      </Button>
                    ) : (
                      <Button
                        size="sm"
                        variant="secondary"
                        className="btn-connector-setup"
                        onClick={() => openSetup(c.kind)}
                      >
                        Set up
                      </Button>
                    )}
                  </div>
                ))}
              </div>

              <Button
                variant="ghost"
                size="sm"
                className="btn-connector-add-custom self-start"
                onClick={() => {
                  setCustom({ ...blankCustom });
                  setFormError(null);
                  setView("custom");
                }}
              >
                <IconPlus size={14} /> Add custom connector
              </Button>
            </>
          )}

          {view === "setup" && (
            <>
              <DialogHeader>
                <DialogTitle>Set up {setupEntry?.display_name ?? setupKind}</DialogTitle>
                <DialogDescription>
                  Register an OAuth app with the provider, set its callback to
                  the redirect URI below, then paste the client id and secret.
                </DialogDescription>
              </DialogHeader>
              <div className="space-y-3">
                {setupEntry?.setup_steps && setupEntry.setup_steps.length > 0 && (
                  <ol className="list-decimal space-y-1 pl-5 text-xs text-muted-foreground">
                    {setupEntry.setup_steps.map((s, i) => (
                      <li key={i}>{s}</li>
                    ))}
                  </ol>
                )}
                {setupEntry?.setup_url && (
                  <a
                    href={setupEntry.setup_url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="connector-setup-link inline-block text-sm text-primary hover:underline"
                  >
                    Open {setupEntry.display_name} developer settings ↗
                  </a>
                )}
                {redirectBox(setupRedirect)}
                <div className="space-y-1.5">
                  <Label htmlFor="setup-client-id">Client ID</Label>
                  <Input
                    id="setup-client-id"
                    value={clientId}
                    onChange={(e) => setClientId(e.target.value)}
                  />
                </div>
                <div className="space-y-1.5">
                  <Label htmlFor="setup-client-secret">Client secret</Label>
                  <Input
                    id="setup-client-secret"
                    type="password"
                    value={clientSecret}
                    onChange={(e) => setClientSecret(e.target.value)}
                  />
                </div>
                {formError && <p className="text-sm text-destructive">{formError}</p>}
              </div>
              <div className="flex items-center justify-between gap-2">
                <Button variant="ghost" size="sm" onClick={() => setView("catalog")}>
                  <IconArrowLeft size={14} /> Back
                </Button>
                <Button
                  size="sm"
                  className="btn-connector-save-client"
                  disabled={busy || !clientId.trim() || !clientSecret.trim()}
                  onClick={saveSetup}
                >
                  {busy ? "Saving…" : "Save"}
                </Button>
              </div>
            </>
          )}

          {view === "custom" && (
            <>
              <DialogHeader>
                <DialogTitle>Add custom connector</DialogTitle>
                <DialogDescription>
                  Define any OAuth2 API. The agent gets one authenticated
                  request tool scoped to the API base.
                </DialogDescription>
              </DialogHeader>
              <div className="space-y-3">
                {redirectBox(browserRedirect)}
                <div className="grid grid-cols-2 gap-2">
                  <div className="space-y-1.5">
                    <Label htmlFor="cc-kind">Slug</Label>
                    <Input
                      id="cc-kind"
                      placeholder="linear"
                      value={custom.kind}
                      onChange={(e) => cu("kind", e.target.value)}
                    />
                  </div>
                  <div className="space-y-1.5">
                    <Label htmlFor="cc-name">Display name</Label>
                    <Input
                      id="cc-name"
                      placeholder="Linear"
                      value={custom.display_name}
                      onChange={(e) => cu("display_name", e.target.value)}
                    />
                  </div>
                </div>
                <div className="space-y-1.5">
                  <Label htmlFor="cc-auth">Authorize URL</Label>
                  <Input
                    id="cc-auth"
                    placeholder="https://provider/oauth/authorize"
                    value={custom.authorize_url}
                    onChange={(e) => cu("authorize_url", e.target.value)}
                  />
                </div>
                <div className="space-y-1.5">
                  <Label htmlFor="cc-token">Token URL</Label>
                  <Input
                    id="cc-token"
                    placeholder="https://provider/oauth/token"
                    value={custom.token_url}
                    onChange={(e) => cu("token_url", e.target.value)}
                  />
                </div>
                <div className="space-y-1.5">
                  <Label htmlFor="cc-api">API base URL</Label>
                  <Input
                    id="cc-api"
                    placeholder="https://api.provider.com"
                    value={custom.api_base}
                    onChange={(e) => cu("api_base", e.target.value)}
                  />
                </div>
                <div className="space-y-1.5">
                  <Label htmlFor="cc-scopes">Scopes (space/comma separated)</Label>
                  <Input
                    id="cc-scopes"
                    placeholder="read write"
                    value={custom.scopes}
                    onChange={(e) => cu("scopes", e.target.value)}
                  />
                </div>
                <label className="flex items-center gap-2 text-sm text-foreground">
                  <input
                    type="checkbox"
                    checked={custom.pkce}
                    onChange={(e) => cu("pkce", e.target.checked)}
                  />
                  Use PKCE
                </label>
                <div className="grid grid-cols-2 gap-2">
                  <div className="space-y-1.5">
                    <Label htmlFor="cc-cid">Client ID</Label>
                    <Input
                      id="cc-cid"
                      value={custom.client_id}
                      onChange={(e) => cu("client_id", e.target.value)}
                    />
                  </div>
                  <div className="space-y-1.5">
                    <Label htmlFor="cc-csec">Client secret</Label>
                    <Input
                      id="cc-csec"
                      type="password"
                      value={custom.client_secret}
                      onChange={(e) => cu("client_secret", e.target.value)}
                    />
                  </div>
                </div>
                {formError && <p className="text-sm text-destructive">{formError}</p>}
              </div>
              <div className="flex items-center justify-between gap-2">
                <Button variant="ghost" size="sm" onClick={() => setView("catalog")}>
                  <IconArrowLeft size={14} /> Back
                </Button>
                <Button
                  size="sm"
                  className="btn-connector-save-custom"
                  disabled={
                    busy ||
                    !custom.kind.trim() ||
                    !custom.authorize_url.trim() ||
                    !custom.token_url.trim() ||
                    !custom.api_base.trim() ||
                    !custom.client_id.trim() ||
                    !custom.client_secret.trim()
                  }
                  onClick={saveCustom}
                >
                  {busy ? "Creating…" : "Create"}
                </Button>
              </div>
            </>
          )}
        </DialogContent>
      </Dialog>
    </div>
  );
}
