import { useEffect, useMemo, useState } from "react";
import { IconGitFork, IconX } from "@tabler/icons-react";
import { useSessionStore, type SessionInfo } from "../stores/sessionStore";
import { Button } from "./ui/button";
import { Input } from "./ui/input";

const API_URL = window.location.origin;

// Prefixes that mark a user-role message as AUTO-INJECTED rather than
// human-typed: bg-task-result deliveries and agent-to-agent reply/question/
// error turns are persisted with role="user" but aren't prompts the user
// would "redo". The fork picker hides them so only genuine human turns are
// offered as rewind targets (mirrors the markers MessageBubble special-cases).
const _AUTO_INJECTED_PREFIXES = [
  "[bg-task-result]",
  "[agent-reply:",
  "[agent-question:",
  "[agent-error:",
];

function isAutoInjectedPrompt(content: unknown): boolean {
  if (typeof content !== "string") return false;
  const t = content.trimStart();
  return _AUTO_INJECTED_PREFIXES.some((p) => t.startsWith(p));
}

export interface ForkSideEffectSummary {
  file_edits: { path: string; turns: number }[];
  bg_tasks: {
    task_id: string | null;
    command: string | null;
    description: string | null;
    status: string;
  }[];
  other_tools: { label: string; count: number }[];
  counts: { total: number; file_edits: number; bg_tasks: number };
}

export interface ForkPreview {
  rewind_to_msg_seq: number;
  prefilled_prompt: string;
  side_effect_summary: ForkSideEffectSummary;
  revert: { available: boolean; refused_reason: string | null };
  can_fork: boolean;
}

interface UserMsg {
  seq: number;
  preview: string;
}

/**
 * Presentational confirm view (session-rewind.md §5.6.2). Pure — takes a
 * fetched preview + controlled checkbox/label, renders the three-class
 * side-effect summary and the single revert affordance (disabled with the
 * preflight reason as a tooltip when revert isn't available). Exported so the
 * unit test can drive it without mocking the fetch flow.
 */
export function ForkConfirmView({
  parentName,
  preview,
  revertChecked,
  onRevertChange,
  label,
  onLabelChange,
}: {
  parentName: string;
  preview: ForkPreview;
  revertChecked: boolean;
  onRevertChange: (v: boolean) => void;
  label: string;
  onLabelChange: (v: string) => void;
}) {
  const s = preview.side_effect_summary;
  const revertAvailable = preview.revert.available;
  const m = preview.rewind_to_msg_seq;
  return (
    <div className="fork-confirm space-y-4" data-testid="fork-confirm">
      <p className="text-sm text-muted-foreground">
        Fork{" "}
        <span className="font-medium text-foreground">{parentName}</span> at
        message <span className="font-medium text-foreground">#{m}</span> —
        rewind to before it and redo it.
      </p>

      {/* Files modified + the single revert affordance */}
      <section className="fork-files rounded-lg border border-border bg-card p-3">
        <div className="flex items-center justify-between">
          <h4 className="text-xs font-semibold text-foreground">
            Files modified ({s.file_edits.length})
          </h4>
          <label
            className={`flex items-center gap-1.5 text-xs ${
              revertAvailable
                ? "text-foreground"
                : "text-muted-foreground/60 cursor-not-allowed"
            }`}
            title={
              revertAvailable
                ? "Restore the working tree to its fork-point state"
                : preview.revert.refused_reason ?? "Revert unavailable"
            }
            data-testid="fork-revert-label"
          >
            <input
              type="checkbox"
              data-testid="fork-revert-checkbox"
              checked={revertChecked && revertAvailable}
              disabled={!revertAvailable}
              onChange={(e) => onRevertChange(e.target.checked)}
            />
            Revert to fork-point state
          </label>
        </div>
        {s.file_edits.length === 0 ? (
          <p className="mt-1 text-xs text-muted-foreground/70">None</p>
        ) : (
          <ul className="mt-1.5 space-y-0.5">
            {s.file_edits.map((f) => (
              <li
                key={f.path}
                className="flex justify-between font-mono text-[11px] text-muted-foreground"
              >
                <span className="truncate">{f.path}</span>
                <span className="shrink-0 pl-2">
                  ({f.turns} turn{f.turns === 1 ? "" : "s"})
                </span>
              </li>
            ))}
          </ul>
        )}
        {!revertAvailable && preview.revert.refused_reason && (
          <p
            className="mt-1.5 text-[11px] text-muted-foreground/70"
            data-testid="fork-revert-reason"
          >
            Revert unavailable: {preview.revert.refused_reason}
          </p>
        )}
      </section>

      {s.bg_tasks.length > 0 && (
        <section className="fork-bg rounded-lg border border-border bg-card p-3">
          <h4 className="text-xs font-semibold text-foreground">
            Background tasks ({s.bg_tasks.length})
          </h4>
          <ul className="mt-1.5 space-y-0.5">
            {s.bg_tasks.map((t, i) => (
              <li
                key={t.task_id ?? i}
                className="flex justify-between text-[11px] text-muted-foreground"
              >
                <span className="truncate font-mono">
                  {t.command ?? t.description ?? "bg task"}
                </span>
                <span className="shrink-0 pl-2">{t.status}</span>
              </li>
            ))}
          </ul>
        </section>
      )}

      {s.other_tools.length > 0 && (
        <section className="fork-other rounded-lg border border-border bg-card p-3">
          <h4 className="text-xs font-semibold text-foreground">
            Other tool activity (not revertible)
          </h4>
          <ul className="mt-1.5 space-y-0.5">
            {s.other_tools.map((o) => (
              <li
                key={o.label}
                className="text-[11px] text-muted-foreground"
              >
                {o.count} {o.label}
              </li>
            ))}
          </ul>
        </section>
      )}

      <div>
        <label className="text-xs text-muted-foreground">Label (optional)</label>
        <Input
          className="mt-1 h-9 text-sm"
          value={label}
          onChange={(e) => onLabelChange(e.target.value)}
          placeholder="Fork label"
        />
      </div>
    </div>
  );
}

/**
 * Full fork flow (session-rewind.md §5.6.2 / §6.1-6.2). Self-sufficient:
 * fetches the session detail to build the user-message picker, then the
 * `/fork-preview` for the chosen message, then POSTs `/fork`. When
 * `initialSeq` is given (the per-message "Fork from here" button) it jumps
 * straight to the confirm step.
 */
export function ForkDialog({
  sessionId,
  parentName,
  initialSeq,
  onClose,
  onForked,
}: {
  sessionId: string;
  parentName: string;
  initialSeq?: number;
  onClose: () => void;
  onForked: (fork: SessionInfo) => void;
}) {
  const token = useSessionStore((s) => s.token);
  const headers = useMemo(
    () => ({ "Content-Type": "application/json", Authorization: `Bearer ${token}` }),
    [token]
  );

  const [userMsgs, setUserMsgs] = useState<UserMsg[]>([]);
  const [selectedSeq, setSelectedSeq] = useState<number | null>(
    initialSeq ?? null
  );
  const [preview, setPreview] = useState<ForkPreview | null>(null);
  const [revertChecked, setRevertChecked] = useState(false);
  const [label, setLabel] = useState("");
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Load the user-message list for the picker (skipped visually when
  // initialSeq is set, but still useful for the row labels).
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch(`${API_URL}/api/sessions/${sessionId}`, {
          headers,
        });
        if (!res.ok) return;
        const data = await res.json();
        if (cancelled) return;
        const msgs: UserMsg[] = (data.messages || [])
          .filter(
            (mm: { role: string; type: string; content: unknown }) =>
              mm.role === "user" &&
              mm.type === "text" &&
              !isAutoInjectedPrompt(mm.content)
          )
          .map((mm: { seq: number; content: unknown }) => ({
            seq: mm.seq,
            preview:
              typeof mm.content === "string"
                ? mm.content.split("\n")[0].slice(0, 60)
                : "(message)",
          }));
        setUserMsgs(msgs);
      } catch {
        /* ignore */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [sessionId, headers]);

  // Fetch the preview whenever a target message is selected.
  useEffect(() => {
    if (selectedSeq === null) return;
    let cancelled = false;
    setPreview(null);
    (async () => {
      try {
        const res = await fetch(
          `${API_URL}/api/sessions/${sessionId}/fork-preview?rewind_to_msg_seq=${selectedSeq}`,
          { headers }
        );
        if (!res.ok) {
          if (!cancelled) setError("Could not load fork preview.");
          return;
        }
        const data: ForkPreview = await res.json();
        if (cancelled) return;
        setPreview(data);
        setRevertChecked(data.revert.available);
      } catch {
        if (!cancelled) setError("Could not load fork preview.");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [selectedSeq, sessionId, headers]);

  const createFork = async () => {
    if (selectedSeq === null || !preview) return;
    setCreating(true);
    setError(null);
    try {
      const res = await fetch(`${API_URL}/api/sessions/${sessionId}/fork`, {
        method: "POST",
        headers,
        body: JSON.stringify({
          rewind_to_msg_seq: selectedSeq,
          revert_files: revertChecked && preview.revert.available,
          label: label.trim() || null,
        }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => null);
        setError(
          body?.detail?.message ?? body?.detail?.reason ?? "Fork failed."
        );
        return;
      }
      const fork: SessionInfo = await res.json();
      onForked(fork);
      onClose();
    } catch {
      setError("Fork failed.");
    } finally {
      setCreating(false);
    }
  };

  const inConfirm = selectedSeq !== null;

  return (
    <div
      className="fork-dialog-overlay fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
      onClick={onClose}
    >
      <div
        className="fork-dialog w-full max-w-md rounded-xl border-[0.7px] border-border bg-background p-5 shadow-xl"
        onClick={(e) => e.stopPropagation()}
        data-testid="fork-dialog"
      >
        <div className="mb-3 flex items-center gap-2">
          <IconGitFork size={18} className="text-primary" />
          <h3 className="flex-1 text-base font-semibold text-foreground">
            {inConfirm ? "Rewind to this message" : "Rewind to a message"}
          </h3>
          <button
            type="button"
            onClick={onClose}
            className="text-muted-foreground hover:text-foreground"
            aria-label="Close"
          >
            <IconX size={16} />
          </button>
        </div>

        {!inConfirm && (
          <div className="fork-picker" data-testid="fork-picker">
            <p className="mb-2 text-xs text-muted-foreground">
              Pick a user message to rewind to and redo:
            </p>
            {userMsgs.length === 0 && (
              <p className="text-xs italic text-muted-foreground/60">
                No user messages to rewind to.
              </p>
            )}
            {/* Scrollable list — long conversations can have many turns. */}
            <div className="max-h-[50vh] space-y-1 overflow-y-auto pr-1">
              {userMsgs.map((u) => (
                <button
                  key={u.seq}
                  type="button"
                  data-fork-seq={u.seq}
                  className="flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left text-sm text-foreground hover:bg-accent"
                  onClick={() => setSelectedSeq(u.seq)}
                >
                  <span className="font-mono text-[11px] text-muted-foreground">
                    #{u.seq}
                  </span>
                  <span className="truncate">{u.preview}</span>
                </button>
              ))}
            </div>
          </div>
        )}

        {inConfirm && !preview && !error && (
          <p className="text-sm text-muted-foreground">Loading…</p>
        )}

        {inConfirm && preview && (
          <ForkConfirmView
            parentName={parentName}
            preview={preview}
            revertChecked={revertChecked}
            onRevertChange={setRevertChecked}
            label={label}
            onLabelChange={setLabel}
          />
        )}

        {error && (
          <p className="mt-3 text-xs text-destructive" data-testid="fork-error">
            {error}
          </p>
        )}

        <div className="mt-4 flex justify-end gap-2">
          <Button variant="ghost" size="sm" onClick={onClose}>
            Cancel
          </Button>
          {inConfirm && (
            <Button
              size="sm"
              className="btn-create-fork"
              disabled={!preview || creating}
              onClick={createFork}
            >
              {creating ? "Rewinding…" : "Rewind"}
            </Button>
          )}
        </div>
      </div>
    </div>
  );
}
