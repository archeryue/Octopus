import { useState } from "react";
import { IconCheck, IconCopy, IconLogout } from "@tabler/icons-react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "./ui/dialog";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "./ui/tabs";
import { Button } from "./ui/button";
import { useSessionStore } from "../stores/sessionStore";

interface Props {
  open: boolean;
  onOpenChange: (next: boolean) => void;
}

/** Additive settings dialog — sits *alongside* the three-section sidebar,
 * doesn't replace it. Holds the things that don't naturally fit as
 * sidebar sections: connection info, account, future notifier targets. */
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
            <TabsTrigger value="notifications" className="flex-1" disabled>
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

          <TabsContent
            value="notifications"
            className="text-sm text-muted-foreground"
          >
            Notification targets (webhook, browser push, email) — coming
            soon. Tracking in <code>docs/future-features.md</code> #5.
          </TabsContent>
        </Tabs>
      </DialogContent>
    </Dialog>
  );
}
