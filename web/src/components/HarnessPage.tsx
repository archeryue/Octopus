import { useCallback, useEffect, useMemo, useState } from "react";
import {
  IconCheck,
  IconCopy,
  IconPlus,
  IconRefresh,
  IconX,
} from "@tabler/icons-react";
import {
  useSessionStore,
  type CredentialInfo,
} from "../stores/sessionStore";
import { PageHeader } from "./PageHeader";
import { Button } from "./ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "./ui/dialog";
import { Input } from "./ui/input";
import { Label } from "./ui/label";

const API = `${window.location.origin}/api/credentials`;

/** Parse a non-OK fetch Response into a short message safe to show the user.
 *
 * If the body is JSON (FastAPI's standard `{detail: ...}` shape), pull
 * `detail`. If it's HTML (Cloudflare 502, nginx error page, etc.), don't
 * dump it into the UI — show the status code with a short hint instead. */
async function friendlyErrorMessage(
  res: Response,
  action: string
): Promise<string> {
  const contentType = res.headers.get("content-type") || "";
  if (contentType.includes("application/json")) {
    try {
      const body = await res.json();
      const detail = body?.detail;
      if (typeof detail === "string" && detail.trim()) return detail;
      if (Array.isArray(detail) && detail.length) {
        return detail
          .map((d: unknown) => String((d as { msg?: string })?.msg ?? d))
          .join("; ");
      }
    } catch {
      // fall through to status-only message
    }
  }
  if (res.status === 502 || res.status === 504) {
    return (
      `Could not ${action} — the Octopus backend didn't respond in time ` +
      `(${res.status} from gateway). Check the server logs for details.`
    );
  }
  if (res.status === 503) {
    return `Could not ${action} — server unavailable (503). Check the server logs.`;
  }
  return `Could not ${action} — HTTP ${res.status}`;
}

// The credential dialog is a small state machine spanning two backends:
//   choose → (Claude: awaiting_code → submitting) | (Codex: device → polling)
type FlowState =
  | { kind: "idle" }
  | { kind: "choose" }
  | { kind: "claude_starting" }
  | { kind: "claude_awaiting_code"; loginId: string; deviceUrl: string }
  | { kind: "claude_submitting" }
  | { kind: "codex_label" }
  | { kind: "codex_starting" }
  | { kind: "codex_device"; loginId: string; url: string; code: string }
  | { kind: "error"; message: string };

export function HarnessPage({
  onToggleSidebar,
}: {
  onToggleSidebar: () => void;
}) {
  const token = useSessionStore((s) => s.token);
  const credentials = useSessionStore((s) => s.credentials);
  const setCredentials = useSessionStore((s) => s.setCredentials);
  // Which agents a credential is bound to — the design puts "in use by Agent"
  // on every card, because that's what makes an expired token urgent.
  const agents = useSessionStore((s) => s.agents);

  const [open, setOpen] = useState(false);
  const [flow, setFlow] = useState<FlowState>({ kind: "idle" });
  const [label, setLabel] = useState("");
  const [code, setCode] = useState("");
  const [copiedCode, setCopiedCode] = useState(false);
  // Set while re-authorizing an existing credential (harness-credential-reauth.md
  // §5): threads the target id through the login flow so the backend updates
  // that row in place + clears its needs_reconnect flag, instead of minting a
  // new credential and stranding every binding on the dead one.
  const [reauthId, setReauthId] = useState<string | null>(null);

  // Memoized for a stable reference — see the same note in SettingsDialog:
  // rebuilt per render it could not be a declared dependency without the
  // effect below re-firing every render.
  const headers = useMemo(
    () => ({
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
    }),
    [token]
  );

  const fetchCredentials = useCallback(async () => {
    try {
      const res = await fetch(API, { headers });
      if (res.ok) {
        const items: CredentialInfo[] = await res.json();
        setCredentials(items);
      }
    } catch {
      // ignore
    }
  }, [headers, setCredentials]);

  useEffect(() => {
    fetchCredentials();
  }, [fetchCredentials]);

  // Replace an existing credential (re-auth) or append a new one, off the
  // freshest store state — works for both fresh sign-in and in-place re-auth.
  const upsertCredential = (c: CredentialInfo) => {
    const cur = useSessionStore.getState().credentials;
    const idx = cur.findIndex((x) => x.id === c.id);
    setCredentials(
      idx >= 0 ? cur.map((x) => (x.id === c.id ? c : x)) : [...cur, c]
    );
  };

  // ----------------------------------------------------------- open / close

  const openChooser = () => {
    setLabel("");
    setCode("");
    setFlow({ kind: "choose" });
    setOpen(true);
  };

  const closeAndReset = () => {
    setOpen(false);
    setLabel("");
    setCode("");
    setCopiedCode(false);
    setReauthId(null);
    setFlow({ kind: "idle" });
  };

  // Re-authorize an existing (expired) credential: jump straight into its
  // backend's login flow with the target id remembered, skipping the chooser.
  const reauthCredential = (c: CredentialInfo) => {
    setReauthId(c.id);
    setLabel(c.label);
    setCode("");
    setCopiedCode(false);
    setOpen(true);
    if (c.backend === "claude-code") {
      void startClaudeLogin();
    } else {
      void startCodexLogin(c.id, c.label);
    }
  };

  // --------------------------------------------------------------- Claude OAuth

  const startClaudeLogin = async () => {
    setFlow({ kind: "claude_starting" });
    try {
      const res = await fetch(`${API}/oauth/start`, {
        method: "POST",
        headers,
        body: JSON.stringify({ backend: "claude-code" }),
      });
      if (!res.ok) {
        setFlow({
          kind: "error",
          message: await friendlyErrorMessage(res, "start login"),
        });
        return;
      }
      const body: { login_id: string; device_url: string } = await res.json();
      setFlow({
        kind: "claude_awaiting_code",
        loginId: body.login_id,
        deviceUrl: body.device_url,
      });
    } catch (e) {
      setFlow({ kind: "error", message: String(e) });
    }
  };

  const submitCode = async () => {
    if (flow.kind !== "claude_awaiting_code") return;
    if (!label.trim() || !code.trim()) {
      setFlow({ kind: "error", message: "Label and code are both required" });
      return;
    }
    const loginId = flow.loginId;
    setFlow({ kind: "claude_submitting" });
    try {
      const res = await fetch(`${API}/oauth/complete`, {
        method: "POST",
        headers,
        body: JSON.stringify({
          login_id: loginId,
          code: code.trim(),
          label: label.trim(),
          // Re-auth in place when set; the backend updates this row + clears
          // its needs_reconnect flag instead of minting a new credential.
          credential_id: reauthId,
        }),
      });
      if (!res.ok) {
        setFlow({
          kind: "error",
          message: await friendlyErrorMessage(res, "complete login"),
        });
        return;
      }
      const created: CredentialInfo = await res.json();
      upsertCredential(created);
      closeAndReset();
    } catch (e) {
      setFlow({ kind: "error", message: String(e) });
    }
  };

  // --------------------------------------------------------------- Codex device

  const startCodexLogin = async (
    reauthCredentialId?: string,
    labelOverride?: string
  ) => {
    const effectiveLabel = (labelOverride ?? label).trim();
    if (!effectiveLabel) {
      setFlow({ kind: "error", message: "A label is required" });
      return;
    }
    setFlow({ kind: "codex_starting" });
    try {
      const res = await fetch(`${API}/codex/start`, {
        method: "POST",
        headers,
        body: JSON.stringify({
          label: effectiveLabel,
          reauth_credential_id: reauthCredentialId ?? null,
        }),
      });
      if (!res.ok) {
        setFlow({
          kind: "error",
          message: await friendlyErrorMessage(res, "start Codex sign-in"),
        });
        return;
      }
      // `start` returns immediately; the URL + code arrive via status polling.
      const body: { login_id: string } = await res.json();
      setFlow({ kind: "codex_device", loginId: body.login_id, url: "", code: "" });
    } catch (e) {
      setFlow({ kind: "error", message: String(e) });
    }
  };

  // While the Codex device step is showing, poll the login status until Codex
  // reports the browser authorization completed (or failed).
  const flowKind = flow.kind;
  const codexLoginId = flow.kind === "codex_device" ? flow.loginId : null;
  useEffect(() => {
    if (flowKind !== "codex_device" || !codexLoginId) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const res = await fetch(`${API}/codex/${codexLoginId}/status`, {
          headers,
        });
        if (cancelled) return;
        if (res.ok) {
          const body: {
            state: string;
            verification_url?: string | null;
            user_code?: string | null;
            message?: string | null;
            credential?: CredentialInfo | null;
          } = await res.json();
          if (body.state === "success" && body.credential) {
            upsertCredential(body.credential);
            closeAndReset();
            return;
          }
          if (body.state === "error" || body.state === "cancelled") {
            setFlow({
              kind: "error",
              message: body.message || "Codex sign-in did not complete.",
            });
            return;
          }
          // Still pending — surface the URL + code as soon as codex emits them.
          if (body.verification_url && body.user_code) {
            setFlow({
              kind: "codex_device",
              loginId: codexLoginId,
              url: body.verification_url,
              code: body.user_code,
            });
          }
        }
      } catch {
        // transient — keep polling
      }
      if (!cancelled) timer = setTimeout(poll, 2000);
    };
    // Poll quickly at first so the code appears promptly, then settle to 2s.
    timer = setTimeout(poll, 600);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [flowKind, codexLoginId]);

  const cancelInflightLogin = async () => {
    if (flow.kind === "codex_device") {
      try {
        await fetch(`${API}/codex/cancel`, {
          method: "POST",
          headers,
          body: JSON.stringify({ login_id: flow.loginId }),
        });
      } catch {
        // best-effort
      }
    }
  };

  const handleOpenChange = (next: boolean) => {
    if (!next) {
      void cancelInflightLogin();
      closeAndReset();
    } else {
      setOpen(true);
    }
  };

  // --------------------------------------------------------------- Delete

  const remove = async (id: string) => {
    try {
      const res = await fetch(`${API}/${id}`, { method: "DELETE", headers });
      if (res.ok) {
        setCredentials(credentials.filter((c) => c.id !== id));
      }
    } catch {
      // ignore
    }
  };

  // --------------------------------------------------------------- Render

  const brokenCount = credentials.filter(
    (c) => c.needs_reconnect
  ).length;
  const agentsUsing = (credentialId: string) =>
    agents.filter((a) => a.credential_id === credentialId).map((a) => a.name);

  return (
    <div className="harness-page flex min-h-0 flex-1 flex-col">
      <PageHeader
        crumbs={["Harness"]}
        meta={
          <>
            {credentials.length} engine credential
            {credentials.length === 1 ? "" : "s"}
            {brokenCount > 0 && ` · ${brokenCount} needs reconnect`}
          </>
        }
        onToggleSidebar={onToggleSidebar}
        actions={
          <Button className="btn-credential-add" size="sm" onClick={openChooser}>
            + Connect engine
          </Button>
        }
      />

      <div className="page-body">
        <div className="credential-list flex flex-col gap-3.5">
          {credentials.map((c) => {
            const broken = c.needs_reconnect;
            const users = agentsUsing(c.id);
            return (
              <div
                key={c.id}
                className={`credential-item group card px-5 py-4 ${
                  broken ? "border-warn-border bg-warn-bg/40" : ""
                }`}
              >
                <div className="flex items-start gap-3.5">
                  <span
                    className={`tile tile-lg credential-badge backend-${c.backend} ${
                      c.backend === "claude-code" ? "tile-warm" : "tile-blue"
                    }`}
                  >
                    {c.backend === "claude-code" ? "◲" : "◼"}
                  </span>
                  <div className="min-w-0 flex-1">
                    <div className="credential-label truncate text-[15px] font-semibold text-gray-950">
                      {c.label}
                    </div>
                    <div className="mt-1 font-mono text-[11.5px] text-gray-700">
                      {c.backend === "claude-code" ? "Claude Code" : "Codex"} ·{" "}
                      {c.auth_type === "oauth" ? "OAuth" : "API key"}
                    </div>
                  </div>
                  <div className="flex shrink-0 items-center gap-2.5">
                    {broken ? (
                      <>
                        <span
                          className="pill pill-warn credential-badge auth-expired"
                          title={
                            c.last_refresh_error_code
                              ? `Sign-in invalid (${c.last_refresh_error_code})`
                              : "Sign-in invalid"
                          }
                        >
                          token expired
                        </span>
                        <button
                          className="btn-credential-reauth pill bg-warn text-white"
                          onClick={() => reauthCredential(c)}
                          title="Re-authorize this credential"
                        >
                          Re-authorize →
                        </button>
                      </>
                    ) : (
                      <span className="pill pill-success">
                        <span className="dot" />
                        Connected
                      </span>
                    )}
                    <button
                      className="btn-delete inline-flex size-8 items-center justify-center rounded-lg text-gray-600 opacity-0 transition-opacity hover:bg-danger-bg hover:text-destructive group-hover:opacity-100"
                      onClick={() => remove(c.id)}
                      title="Delete credential"
                      aria-label={`Delete ${c.label}`}
                    >
                      <IconX size={15} />
                    </button>
                  </div>
                </div>

                <div className="mt-4 flex flex-wrap items-baseline gap-x-10 gap-y-2 border-t border-gray-300 pt-3.5">
                  <div>
                    <div className="font-mono text-[11px] text-gray-700">
                      in use by Agent
                    </div>
                    <div className="mt-1 text-[13px] text-gray-900">
                      {users.length ? users.join(" · ") : "—"}
                    </div>
                  </div>
                  {broken && users.length > 0 && (
                    <div className="ml-auto text-[12.5px] text-warn-foreground">
                      ⚠ {users.length} agent{users.length === 1 ? "" : "s"} can't
                      start new sessions
                    </div>
                  )}
                </div>
              </div>
            );
          })}

          {credentials.length === 0 && (
            <button
              type="button"
              className="btn-credential-add-card flex min-h-[120px] flex-col items-center justify-center gap-2.5 rounded-xl border border-dashed border-gray-400 text-gray-700 transition-colors hover:border-primary-200 hover:text-primary"
              onClick={openChooser}
            >
              <span className="inline-flex size-8 items-center justify-center rounded-lg bg-gray-100">
                <IconPlus size={16} />
              </span>
              <span className="text-[13px]">
                Sign in to Claude Code or Codex
              </span>
            </button>
          )}
        </div>

        <div className="mt-5 flex items-start gap-3 rounded-xl border border-primary-100 bg-primary-50/60 px-5 py-4">
          <span className="tile tile-lg bg-primary text-white">
            <IconRefresh size={15} />
          </span>
          <p className="text-[13px] leading-relaxed text-gray-900">
            <span className="font-semibold text-primary">
              Engines are decoupled from agents.
            </span>{" "}
            Memory, sessions and history live on the agent — swap an agent from
            Claude Code to Codex without losing any of it.
          </p>
        </div>
      </div>

      <Dialog open={open} onOpenChange={handleOpenChange}>
        <DialogContent className="credential-dialog">
          <DialogHeader>
            <DialogTitle>
              {flow.kind === "codex_label" ||
              flow.kind === "codex_starting" ||
              flow.kind === "codex_device"
                ? "Sign in with Codex"
                : flow.kind === "choose"
                ? "Add a credential"
                : "Sign in with Claude Code"}
            </DialogTitle>
            <DialogDescription>
              {flow.kind === "choose"
                ? "Connect an AI backend so its sessions can authenticate."
                : flow.kind === "codex_label" ||
                  flow.kind === "codex_starting" ||
                  flow.kind === "codex_device"
                ? "Authorize Octopus with your ChatGPT account on any device."
                : "Octopus stores the resulting long-lived API key encrypted at rest."}
            </DialogDescription>
          </DialogHeader>

          {/* Step 0 — choose backend */}
          {flow.kind === "choose" && (
            <div className="credential-choose grid grid-cols-2 gap-3">
              <button
                type="button"
                className="btn-choose-claude flex flex-col items-start gap-1 rounded-lg border border-border p-4 text-left hover:border-primary/60 hover:bg-accent/40 transition-colors"
                onClick={startClaudeLogin}
              >
                <span className="text-sm font-semibold text-foreground">
                  Claude Code
                </span>
                <span className="text-xs text-muted-foreground">
                  Sign in with Anthropic (OAuth).
                </span>
              </button>
              <button
                type="button"
                className="btn-choose-codex flex flex-col items-start gap-1 rounded-lg border border-border p-4 text-left hover:border-primary/60 hover:bg-accent/40 transition-colors"
                onClick={() => setFlow({ kind: "codex_label" })}
              >
                <span className="text-sm font-semibold text-foreground">
                  Codex
                </span>
                <span className="text-xs text-muted-foreground">
                  Sign in with ChatGPT (device code).
                </span>
              </button>
            </div>
          )}

          {(flow.kind === "claude_starting" ||
            flow.kind === "codex_starting") && (
            <div className="text-sm text-muted-foreground">
              {flow.kind === "codex_starting"
                ? "Asking Codex for a device code…"
                : "Preparing OAuth login…"}
            </div>
          )}

          {/* Codex — collect a label before starting (needed at sign-in time) */}
          {flow.kind === "codex_label" && (
            <div className="space-y-3">
              <div className="space-y-1.5">
                <Label htmlFor="codex-label">Label</Label>
                <Input
                  id="codex-label"
                  placeholder="e.g. ChatGPT Plus"
                  value={label}
                  onChange={(e) => setLabel(e.target.value)}
                  autoFocus
                />
              </div>
            </div>
          )}

          {/* Codex — waiting on codex to fetch the device code */}
          {flow.kind === "codex_device" && !flow.url && (
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <span className="inline-block size-2 rounded-full bg-primary animate-pulse" />
              Asking Codex for a device code…
            </div>
          )}

          {/* Codex — device code displayed; polling for authorization */}
          {flow.kind === "codex_device" && flow.url && (
            <div className="space-y-4">
              <div>
                <div className="text-sm text-foreground mb-2">
                  <span className="font-semibold">Step 1.</span> Open this link
                  and sign in to ChatGPT:
                </div>
                <a
                  className="codex-device-url block break-all rounded-md border border-dashed border-border bg-muted/40 px-3 py-2 text-xs font-mono text-primary hover:underline"
                  href={flow.url}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  {flow.url}
                </a>
              </div>
              <div>
                <div className="text-sm text-foreground mb-2">
                  <span className="font-semibold">Step 2.</span> Enter this
                  one-time code:
                </div>
                <div className="flex items-center gap-2">
                  <code className="codex-user-code flex-1 rounded-md border border-border bg-input px-3 py-2 text-center text-lg font-mono font-semibold tracking-widest text-foreground">
                    {flow.code}
                  </code>
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    className="btn-copy-code"
                    onClick={() => {
                      navigator.clipboard?.writeText(flow.code).catch(() => {});
                      setCopiedCode(true);
                      setTimeout(() => setCopiedCode(false), 1500);
                    }}
                  >
                    {copiedCode ? <IconCheck size={16} /> : <IconCopy size={16} />}
                  </Button>
                </div>
              </div>
              <div className="flex items-center gap-2 text-sm text-muted-foreground">
                <span className="inline-block size-2 rounded-full bg-primary animate-pulse" />
                Waiting for you to authorize in the browser…
              </div>
            </div>
          )}

          {/* Claude — paste the code from the callback page */}
          {flow.kind === "claude_awaiting_code" && (
            <div className="space-y-4">
              <div>
                <div className="text-sm text-foreground mb-2">
                  <span className="font-semibold">Step 1.</span>{" "}
                  Open this URL, sign in, then copy the code shown.
                </div>
                <a
                  className="credential-device-url block break-all rounded-md border border-dashed border-border bg-muted/40 px-3 py-2 text-xs font-mono text-primary hover:underline"
                  href={flow.deviceUrl}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  {flow.deviceUrl}
                </a>
              </div>

              <div>
                <div className="text-sm text-foreground mb-2">
                  <span className="font-semibold">Step 2.</span>{" "}
                  Name this account and paste the code:
                </div>
                <div className="space-y-3">
                  <div className="space-y-1.5">
                    <Label htmlFor="cred-label">Label</Label>
                    <Input
                      id="cred-label"
                      placeholder="e.g. Personal"
                      value={label}
                      onChange={(e) => setLabel(e.target.value)}
                    />
                  </div>
                  <div className="space-y-1.5">
                    <Label htmlFor="cred-code">Code from browser</Label>
                    <Input
                      id="cred-code"
                      placeholder="paste here"
                      value={code}
                      onChange={(e) => setCode(e.target.value)}
                    />
                  </div>
                </div>
              </div>
            </div>
          )}

          {flow.kind === "claude_submitting" && (
            <div className="text-sm text-muted-foreground">
              Exchanging code for token…
            </div>
          )}

          {flow.kind === "error" && (
            <div className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
              {flow.message}
            </div>
          )}

          <DialogFooter>
            {flow.kind === "error" ? (
              <Button variant="outline" onClick={() => setFlow({ kind: "choose" })}>
                Back
              </Button>
            ) : (
              <Button variant="ghost" onClick={() => handleOpenChange(false)}>
                Cancel
              </Button>
            )}
            {flow.kind === "codex_label" && (
              <Button
                className="btn-codex-start"
                onClick={() => startCodexLogin()}
                disabled={!label.trim()}
              >
                Continue
              </Button>
            )}
            {flow.kind === "claude_awaiting_code" && (
              <Button
                className="btn-cred-submit"
                onClick={submitCode}
                disabled={!label.trim() || !code.trim()}
              >
                Finish sign-in
              </Button>
            )}
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
