import { useCallback, useEffect, useState } from "react";
import { IconPlus, IconX } from "@tabler/icons-react";
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

type FlowState =
  | { kind: "idle" }
  | { kind: "starting" }
  | { kind: "awaiting_code"; loginId: string; deviceUrl: string }
  | { kind: "submitting" }
  | { kind: "error"; message: string };

export function CredentialList() {
  const token = useSessionStore((s) => s.token);
  const credentials = useSessionStore((s) => s.credentials);
  const setCredentials = useSessionStore((s) => s.setCredentials);

  const [open, setOpen] = useState(false);
  const [flow, setFlow] = useState<FlowState>({ kind: "idle" });
  const [label, setLabel] = useState("");
  const [code, setCode] = useState("");

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

  // --------------------------------------------------------------- OAuth flow

  const startLogin = async () => {
    setOpen(true);
    setFlow({ kind: "starting" });
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
        kind: "awaiting_code",
        loginId: body.login_id,
        deviceUrl: body.device_url,
      });
    } catch (e) {
      setFlow({ kind: "error", message: String(e) });
    }
  };

  const submitCode = async () => {
    if (flow.kind !== "awaiting_code") return;
    if (!label.trim() || !code.trim()) {
      setFlow({ kind: "error", message: "Label and code are both required" });
      return;
    }
    const loginId = flow.loginId;
    setFlow({ kind: "submitting" });
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

  const cancelInflightLogin = async () => {
    if (flow.kind === "awaiting_code") {
      const loginId = flow.loginId;
      try {
        await fetch(`${API}/oauth/cancel`, {
          method: "POST",
          headers,
          body: JSON.stringify({ login_id: loginId }),
        });
      } catch {
        // best-effort
      }
    }
  };

  const closeAndReset = () => {
    setOpen(false);
    setLabel("");
    setCode("");
    setFlow({ kind: "idle" });
  };

  const handleOpenChange = (next: boolean) => {
    if (!next) {
      // Closing the dialog mid-flow should cancel any in-flight login
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
    <div className="credential-section shrink-0 pb-3 pt-2">
      <div className="credential-header group flex h-8 items-center justify-between rounded-lg pl-2 pr-1 hover:bg-sidebar-accent transition-colors">
        <span className="credential-title text-[13px] font-medium leading-4 text-sidebar-foreground/50 group-hover:text-sidebar-foreground transition-colors">
          Harness
        </span>
        <button
          className="btn-credential-add inline-flex h-6 w-6 items-center justify-center rounded-md text-sidebar-foreground/70 hover:bg-[hsl(var(--gray-200))] hover:text-sidebar-foreground transition-colors"
          onClick={startLogin}
          title="Sign in"
          aria-label="Sign in"
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
            <span
              className={`credential-badge backend-${c.backend} text-[10px] font-medium uppercase tracking-wider px-1.5 py-0.5 rounded shrink-0 ${
                c.backend === "claude-code"
                  ? "bg-primary-100 text-primary-700"
                  : "bg-secondary text-secondary-foreground"
              }`}
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
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Sign in with Claude Code</DialogTitle>
            <DialogDescription>
              Octopus stores the resulting long-lived API key encrypted at rest.
            </DialogDescription>
          </DialogHeader>

          {flow.kind === "starting" && (
            <div className="text-sm text-muted-foreground">
              Preparing OAuth login…
            </div>
          )}

          {flow.kind === "awaiting_code" && (
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

          {flow.kind === "submitting" && (
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
              <Button
                variant="outline"
                onClick={() => setFlow({ kind: "idle" })}
              >
                Try again
              </Button>
            ) : (
              <Button
                variant="ghost"
                onClick={() => handleOpenChange(false)}
              >
                Cancel
              </Button>
            )}
            {flow.kind === "awaiting_code" && (
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
