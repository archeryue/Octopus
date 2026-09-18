import { useCallback, useEffect, useState } from "react";
import {
  IconCheck,
  IconCopy,
  IconKey,
  IconLogout,
  IconPlus,
  IconX,
} from "@tabler/icons-react";
import type { NotifierInfo } from "../api";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "./ui/dialog";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "./ui/tabs";
import { Button } from "./ui/button";
import { Input } from "./ui/input";
import { Label } from "./ui/label";
import { useSessionStore } from "../stores/sessionStore";

interface Props {
  open: boolean;
  onOpenChange: (next: boolean) => void;
}

const API = `${window.location.origin}/api`;

/** Additive settings dialog — sits *alongside* the three-section sidebar,
 * doesn't replace it. Holds the things that don't naturally fit as
 * sidebar sections: connection info, account, notifier targets. */
export function SettingsDialog({ open, onOpenChange }: Props) {
  const token = useSessionStore((s) => s.token);
  const setToken = useSessionStore((s) => s.setToken);
  const [copied, setCopied] = useState(false);

  const serverUrl =
    typeof window !== "undefined" ? window.location.origin : "";
  const appVersion = "0.1.0";

  const copyToken = () => {
    navigator.clipboard?.writeText(token).catch(() => {});
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };

  const signOut = () => {
    setToken("");
    onOpenChange(false);
    window.location.reload();
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="settings-dialog max-w-xl">
        <DialogHeader>
          <DialogTitle>Settings</DialogTitle>
          <DialogDescription>
            Connection, account, and notification preferences.
          </DialogDescription>
        </DialogHeader>

        <Tabs defaultValue="general">
          <TabsList className="w-full">
            <TabsTrigger value="general" className="flex-1">
              General
            </TabsTrigger>
            <TabsTrigger value="account" className="flex-1">
              Account
            </TabsTrigger>
            <TabsTrigger value="notifications" className="flex-1">
              Notifications
            </TabsTrigger>
          </TabsList>

          <TabsContent value="general" className="space-y-4">
            <div className="settings-row space-y-1.5">
              <div className="settings-label text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                Server
              </div>
              <div className="settings-value text-sm text-foreground font-mono break-all">
                {serverUrl}
              </div>
            </div>
            <div className="settings-row space-y-1.5">
              <div className="settings-label text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                Version
              </div>
              <div className="settings-value text-sm text-foreground font-mono">
                {appVersion}
              </div>
            </div>
          </TabsContent>

          <TabsContent value="account" className="space-y-4">
            <div className="settings-row space-y-1.5">
              <div className="settings-label text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                Access token
              </div>
              <div className="flex items-center gap-2">
                <div className="flex-1 text-sm text-foreground font-mono break-all rounded-lg border-[0.7px] border-gray-400 px-3 py-2 bg-input">
                  {token || <span className="text-muted-foreground">(none)</span>}
                </div>
                <Button
                  className="btn-copy-token"
                  variant="outline"
                  size="sm"
                  onClick={copyToken}
                  disabled={!token}
                >
                  {copied ? (
                    <>
                      <IconCheck size={16} />
                      Copied
                    </>
                  ) : (
                    <>
                      <IconCopy size={16} />
                      Copy
                    </>
                  )}
                </Button>
              </div>
            </div>
            <RotateTokenPanel token={token} />

            <div className="settings-row pt-2 border-t border-border">
              <Button
                className="btn-signout"
                variant="outline"
                onClick={signOut}
              >
                <IconLogout size={16} />
                Sign out
              </Button>
            </div>
          </TabsContent>

          <TabsContent value="notifications">
            <NotifierPanel token={token} />
          </TabsContent>
        </Tabs>
      </DialogContent>
    </Dialog>
  );
}

// ---------------------------------------------------------------------------
// Notifier panel — list / add / delete webhook targets
// ---------------------------------------------------------------------------

/** Change the access token (token-rotation.md).
 *
 * One button, because rotating is one operation: the server re-encrypts every
 * stored secret with the new key, writes it to the env file it actually reads,
 * swaps its own live setting and hands the new token to the clients already
 * signed in — so this tab, and every other one, carries on without a
 * re-login. No file to edit, no restart, nothing to keep in sync by hand.
 */
function RotateTokenPanel({ token }: { token: string }) {
  const [next, setNext] = useState("");
  const [revoke, setRevoke] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);

  const rotate = async () => {
    if (!next.trim() || busy) return;
    setBusy(true);
    setError(null);
    setDone(null);
    try {
      const res = await fetch(`${window.location.origin}/api/auth/rotate`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({
          new_token: next.trim(),
          revoke_other_clients: revoke,
        }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => null);
        throw new Error(body?.detail || `HTTP ${res.status}`);
      }
      const body = (await res.json()) as {
        env_files: string[];
        reencrypted: Record<string, number>;
      };
      const secrets = Object.values(body.reencrypted).reduce((a, b) => a + b, 0);
      // The token this client uses arrives over the WebSocket, so by the time
      // this renders the tab is already on the new one.
      setDone(
        `Rotated. ${secrets} stored secret${secrets === 1 ? "" : "s"} re-encrypted; ` +
          `wrote ${body.env_files.join(", ")}.`
      );
      setNext("");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Rotation failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="settings-row rotate-token space-y-1.5 border-t border-border pt-3">
      <div className="settings-label text-xs font-semibold uppercase tracking-wider text-muted-foreground">
        Change token
      </div>
      <p className="text-xs leading-relaxed text-muted-foreground">
        Re-encrypts every stored credential with the new token and updates the
        env file — no restart. Signed-in devices keep working unless you revoke
        them.
      </p>
      <div className="flex items-center gap-2">
        <Input
          className="input-new-token flex-1 font-mono"
          type="password"
          autoComplete="new-password"
          placeholder="New token (12+ characters)"
          value={next}
          onChange={(e) => setNext(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") rotate();
          }}
        />
        <Button
          className="btn-rotate-token"
          variant="outline"
          size="sm"
          onClick={rotate}
          disabled={busy || !next.trim()}
        >
          <IconKey size={16} />
          {busy ? "Rotating…" : "Rotate"}
        </Button>
      </div>
      <label className="flex items-center gap-2 text-xs text-muted-foreground">
        <input
          type="checkbox"
          className="input-revoke-others"
          checked={revoke}
          onChange={(e) => setRevoke(e.target.checked)}
        />
        Sign out other devices (use when the old token leaked)
      </label>
      {error && (
        <p className="rotate-token-error text-xs text-destructive">{error}</p>
      )}
      {done && <p className="rotate-token-done text-xs text-success">{done}</p>}
    </div>
  );
}

function NotifierPanel({ token }: { token: string }) {
  const [items, setItems] = useState<NotifierInfo[]>([]);
  const [showForm, setShowForm] = useState(false);
  const [label, setLabel] = useState("");
  const [url, setUrl] = useState("");
  const [error, setError] = useState<string | null>(null);

  const headers = {
    "Content-Type": "application/json",
    Authorization: `Bearer ${token}`,
  };

  const fetchItems = useCallback(async () => {
    try {
      const res = await fetch(`${API}/notifiers`, { headers });
      if (res.ok) setItems(await res.json());
    } catch {
      // ignore
    }
  }, [token]);

  useEffect(() => {
    fetchItems();
  }, [fetchItems]);

  const create = async () => {
    setError(null);
    if (!label.trim() || !url.trim()) {
      setError("Both label and URL are required.");
      return;
    }
    try {
      const res = await fetch(`${API}/notifiers`, {
        method: "POST",
        headers,
        body: JSON.stringify({
          type: "webhook",
          label: label.trim(),
          config: { url: url.trim() },
        }),
      });
      if (!res.ok) {
        setError(`Server returned ${res.status}.`);
        return;
      }
      const created: NotifierInfo = await res.json();
      setItems([...items, created]);
      setLabel("");
      setUrl("");
      setShowForm(false);
    } catch (e) {
      setError(String(e));
    }
  };

  const remove = async (id: string) => {
    try {
      const res = await fetch(`${API}/notifiers/${id}`, {
        method: "DELETE",
        headers,
      });
      if (res.ok) setItems(items.filter((n) => n.id !== id));
    } catch {
      // ignore
    }
  };

  return (
    <div className="notifier-panel space-y-3">
      <div className="text-xs text-muted-foreground leading-relaxed">
        Webhook targets receive a POST with{" "}
        <code className="font-mono">
          {"{type, title, message, session_id, session_name}"}
        </code>{" "}
        when a session goes idle.
      </div>

      <div className="notifier-list flex flex-col gap-1">
        {items.length === 0 && !showForm && (
          <div className="text-sm text-muted-foreground italic px-1 py-2">
            No notifier targets configured.
          </div>
        )}
        {items.map((n) => (
          <div
            key={n.id}
            className="notifier-item group flex items-center gap-2 rounded-lg border-[0.7px] border-border px-3 py-2"
          >
            <span className="text-[10px] font-semibold uppercase tracking-wider text-primary-700 bg-primary-100 px-1.5 py-0.5 rounded">
              {n.type}
            </span>
            <span className="flex-1 min-w-0">
              <span className="block text-sm text-foreground truncate">
                {n.label}
              </span>
              <span className="block text-xs text-muted-foreground font-mono truncate">
                {(n.config as { url?: string }).url || ""}
              </span>
            </span>
            <button
              className="btn-delete inline-flex h-6 w-6 items-center justify-center rounded-md text-muted-foreground hover:bg-destructive/10 hover:text-destructive opacity-0 group-hover:opacity-100 transition-opacity"
              onClick={() => remove(n.id)}
              title="Delete notifier"
            >
              <IconX size={14} />
            </button>
          </div>
        ))}
      </div>

      {!showForm && (
        <Button
          variant="outline"
          size="sm"
          className="btn-notifier-add"
          onClick={() => setShowForm(true)}
        >
          <IconPlus size={14} />
          Add webhook
        </Button>
      )}

      {showForm && (
        <div className="notifier-form rounded-lg border-[0.7px] border-border bg-card p-4 space-y-3">
          <div className="space-y-1.5">
            <Label htmlFor="notifier-label">Label</Label>
            <Input
              id="notifier-label"
              placeholder="e.g. ntfy.sh / Slack hook"
              value={label}
              onChange={(e) => setLabel(e.target.value)}
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="notifier-url">Webhook URL</Label>
            <Input
              id="notifier-url"
              placeholder="https://example.com/hook"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
            />
          </div>
          {error && (
            <div className="text-xs text-destructive">{error}</div>
          )}
          <div className="flex justify-end gap-2">
            <Button
              variant="ghost"
              size="sm"
              onClick={() => {
                setShowForm(false);
                setError(null);
              }}
            >
              Cancel
            </Button>
            <Button
              className="btn-notifier-create"
              size="sm"
              onClick={create}
            >
              Save
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
