import { useState } from "react";
import { IconHelpCircle } from "@tabler/icons-react";
import type { PendingQuestion } from "../stores/sessionStore";
import { Button } from "./ui/button";
import { Input } from "./ui/input";

export interface AnswerPayload {
  selected?: string[];
  text?: string;
}

interface Props {
  question: PendingQuestion;
  onSubmit: (questionId: string, answers: AnswerPayload[]) => void;
}

export function QuestionPrompt({ question, onSubmit }: Props) {
  // For each sub-question, hold either selected option labels or a free-text
  // "Other" answer. `selected` is an array even for single-select so the
  // submit serializer is uniform.
  const [answers, setAnswers] = useState<AnswerPayload[]>(() =>
    question.questions.map(() => ({ selected: [] }))
  );

  const setSelected = (i: number, labels: string[]) => {
    setAnswers((prev) => {
      const next = [...prev];
      next[i] = { selected: labels, text: undefined };
      return next;
    });
  };

  const setText = (i: number, text: string) => {
    setAnswers((prev) => {
      const next = [...prev];
      next[i] = { text, selected: undefined };
      return next;
    });
  };

  const toggleMulti = (i: number, label: string) => {
    const cur = answers[i]?.selected || [];
    const exists = cur.includes(label);
    setSelected(i, exists ? cur.filter((l) => l !== label) : [...cur, label]);
  };

  const canSubmit = answers.every((a) => {
    if (a.text && a.text.trim()) return true;
    return (a.selected?.length || 0) > 0;
  });

  const handleSubmit = () => {
    if (!canSubmit) return;
    onSubmit(question.question_id, answers);
  };

  return (
    <div className="msg msg-question rounded-lg border-[0.7px] border-primary/30 bg-card overflow-hidden">
      <div className="question-header flex items-center gap-2.5 px-5 py-4 bg-primary-50 text-sm text-foreground">
        <IconHelpCircle size={18} className="text-primary shrink-0" />
        <strong>Claude is asking</strong>
      </div>
      <div className="question-body px-5 py-5 space-y-5">
        {question.questions.map((q, i) => {
          const multi = !!q.multiSelect;
          const selected = answers[i]?.selected || [];
          const inputName = `q-${question.question_id}-${i}`;
          return (
            <div className="question-item space-y-3" key={i}>
              {q.header && (
                <div className="question-tag text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
                  {q.header}
                </div>
              )}
              <div className="question-text text-sm font-medium text-foreground leading-relaxed">
                {q.question}
              </div>
              <div className="question-options flex flex-col gap-2">
                {q.options.map((opt, j) => {
                  const isSelected = selected.includes(opt.label);
                  return (
                    <label
                      className={`question-option flex items-start gap-3 px-4 py-3 rounded-lg border-[0.7px] cursor-pointer transition-colors ${
                        isSelected
                          ? "selected border-primary bg-primary-50"
                          : "border-border hover:bg-accent"
                      }`}
                      key={j}
                    >
                      <input
                        type={multi ? "checkbox" : "radio"}
                        name={inputName}
                        checked={isSelected}
                        onChange={() =>
                          multi
                            ? toggleMulti(i, opt.label)
                            : setSelected(i, [opt.label])
                        }
                        className="mt-0.5 accent-primary"
                      />
                      <div className="min-w-0 flex-1">
                        <div className="question-option-label text-sm text-foreground">
                          {opt.label}
                        </div>
                        {opt.description && (
                          <div className="question-option-desc text-xs text-muted-foreground mt-1 leading-relaxed">
                            {opt.description}
                          </div>
                        )}
                      </div>
                    </label>
                  );
                })}
              </div>
              <Input
                type="text"
                className="question-other text-sm"
                placeholder="Or type your own answer…"
                value={answers[i]?.text || ""}
                onChange={(e) => setText(i, e.target.value)}
              />
            </div>
          );
        })}
      </div>
      <div className="question-actions flex justify-end gap-2 px-5 py-3 border-t border-border">
        <Button
          className="btn btn-approve"
          onClick={handleSubmit}
          disabled={!canSubmit}
        >
          Submit
        </Button>
      </div>
    </div>
  );
}
