import { useState } from "react";
import type { PendingQuestion } from "../stores/sessionStore";

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
    <div className="msg msg-question">
      <div className="question-header">
        <span className="question-icon">?</span>
        <strong>Claude is asking</strong>
      </div>
      <div className="question-body">
        {question.questions.map((q, i) => {
          const multi = !!q.multiSelect;
          const selected = answers[i]?.selected || [];
          const inputName = `q-${question.question_id}-${i}`;
          return (
            <div className="question-item" key={i}>
              {q.header && <div className="question-tag">{q.header}</div>}
              <div className="question-text">{q.question}</div>
              <div className="question-options">
                {q.options.map((opt, j) => {
                  const isSelected = selected.includes(opt.label);
                  return (
                    <label
                      className={`question-option ${isSelected ? "selected" : ""}`}
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
                      />
                      <span className="question-option-label">{opt.label}</span>
                      {opt.description && (
                        <span className="question-option-desc">
                          {opt.description}
                        </span>
                      )}
                    </label>
                  );
                })}
              </div>
              <input
                type="text"
                className="question-other"
                placeholder="Or type your own answer…"
                value={answers[i]?.text || ""}
                onChange={(e) => setText(i, e.target.value)}
              />
            </div>
          );
        })}
      </div>
      <div className="question-actions">
        <button
          className="btn btn-approve"
          onClick={handleSubmit}
          disabled={!canSubmit}
        >
          Submit
        </button>
      </div>
    </div>
  );
}
