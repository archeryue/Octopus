import { useCallback, useEffect, useState } from "react";
import {
  IconCheck,
  IconCopy,
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
