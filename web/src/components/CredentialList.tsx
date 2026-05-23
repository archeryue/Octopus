import { useCallback, useEffect, useState } from "react";
import { IconCheck, IconCopy, IconPlus, IconX } from "@tabler/icons-react";
import {
  useSessionStore,
  type CredentialInfo,
} from "../stores/sessionStore";
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

export function CredentialList() {
  const token = useSessionStore((s) => s.token);
  const credentials = useSessionStore((s) => s.credentials);
  const setCredentials = useSessionStore((s) => s.setCredentials);

  const [open, setOpen] = useState(false);
  const [flow, setFlow] = useState<FlowState>({ kind: "idle" });
  const [label, setLabel] = useState("");
  const [code, setCode] = useState("");
  const [copiedCode, setCopiedCode] = useState(false);

  const headers = {
    "Content-Type": "application/json",
    Authorization: `Bearer ${token}`,
  };

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
  }, [token, setCredentials]);

  useEffect(() => {
    fetchCredentials();
  }, [fetchCredentials]);

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
    setFlow({ kind: "idle" });
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
      setCredentials([...credentials, created]);
      closeAndReset();
    } catch (e) {
      setFlow({ kind: "error", message: String(e) });
    }
  };

  // --------------------------------------------------------------- Codex device

  const startCodexLogin = async () => {
    if (!label.trim()) {
      setFlow({ kind: "error", message: "A label is required" });
      return;
    }
    setFlow({ kind: "codex_starting" });
    try {
      const res = await fetch(`${API}/codex/start`, {
        method: "POST",
        headers,
        body: JSON.stringify({ label: label.trim() }),
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
            setCredentials([
              ...useSessionStore.getState().credentials,
              body.credential,
            ]);
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

  return (
    <div className="credential-section shrink-0">
      <div className="credential-header group flex h-8 items-center justify-between rounded-lg px-2 hover:bg-sidebar-accent transition-colors">
        <span className="credential-title text-[13px] font-medium leading-4 text-sidebar-foreground/50 group-hover:text-sidebar-foreground transition-colors uppercase tracking-wide">
          Harness
        </span>
        <button
          className="btn-credential-add inline-flex h-6 w-6 items-center justify-center rounded-md text-sidebar-foreground/70 hover:bg-[hsl(var(--gray-200))] hover:text-sidebar-foreground transition-colors"
          onClick={openChooser}
          title="Add credential"
          aria-label="Add credential"
        >
          <IconPlus size={14} />
        </button>
      </div>

      <div className="credential-list flex flex-col gap-0.5 mt-1">
        {credentials.map((c) => (
          <div
            className="credential-item group flex items-center gap-2 rounded-lg px-2 py-1.5 text-sm text-sidebar-foreground hover:bg-sidebar-accent transition-colors"
            key={c.id}
          >
            {/* Both Claude and Codex are harness backends → same blue badge.
                (Connectors use the gray/secondary badge to set them apart.) */}
            <span
              className={`credential-badge backend-${c.backend} text-[10px] font-medium uppercase tracking-wider px-1.5 py-0.5 rounded shrink-0 bg-primary-100 text-primary-700`}
            >
              {c.backend === "claude-code" ? "Claude" : "Codex"}
            </span>
            <span className="credential-label truncate flex-1">{c.label}</span>
            <span
              className={`credential-badge auth-${c.auth_type} text-[10px] font-medium uppercase tracking-wider shrink-0 ${
                c.auth_type === "oauth"
                  ? "text-primary-700"
                  : "text-sidebar-foreground/50"
              }`}
              title={c.auth_type === "oauth" ? "Signed in" : "API key"}
            >
              {c.auth_type === "oauth" ? "OAuth" : "Key"}
            </span>
            <button
              className="btn-delete inline-flex h-6 w-6 items-center justify-center rounded-md text-sidebar-foreground/60 opacity-0 group-hover:opacity-100 transition-opacity hover:bg-destructive/10 hover:text-destructive"
              onClick={() => remove(c.id)}
              title="Delete credential"
            >
              <IconX size={14} />
            </button>
          </div>
        ))}
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
                onClick={startCodexLogin}
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
