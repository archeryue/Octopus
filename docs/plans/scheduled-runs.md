# Scheduled runs: what a fire is, and what stops it stacking

> **Implementation status: SHIPPED.** The overlap guard and the scheduled-turn
> marker in `server/scheduler.py`, `schedules.last_run_session_id`, and the
> clickable "last run" row on the Schedules page. Tests: `tests/test_scheduler.py`,
> `SchedulesPage.test.tsx`.
>
> Octopus already had durable scheduling (APScheduler + a `schedules` table +
> a real session turn). This is the two things it was missing, both found by
> comparing against how VM0 fires its schedules.

## 1. Two modes, unchanged

A fire runs the schedule's prompt in one of two places (agent-refactor.md
§5.3/§5.6):

* **the origin session**, when the schedule came from a `/schedule` chat
  command and that conversation is still live — the result lands where the
  user already is; or
* **a throwaway session** under the agent, auto-archived on idle, when there
  is no origin (continuity across fires comes from agent memory).

## 2. A schedule never has two runs outstanding

A schedule that runs every 5 minutes and takes 10 used to start a second run
on top of the first, then a third — until the box gave out. Nothing noticed,
because each fire was its own session.

The guard is per mode, because "busy" means different things:

* **Throwaway mode**: the previous fire's session is recorded
  (`last_run_session_id`, written *before* the turn starts so an in-flight fire
  is visible to the next tick). If that session is still running or has a
  queue, the tick is skipped and logged. A session that is gone, or idle, is
  never a reason to skip — the guard must not be able to wedge a schedule.
* **Origin mode**: queuing behind the *user's* turn is the design, so a
  running session is fine. Queuing behind a *previous fire* is a pile-up, so a
  tick is skipped when the session already has something waiting.

Skipping keeps the schedule's cadence rather than shifting it: the next tick
tries again. (VM0 answers the same situation with "Previous run is still
active"; the difference is that Octopus can queue in the chat case, so it only
refuses the second one.)

## 3. A scheduled turn says it is one

Every other machine-injected turn in Octopus announces itself —
`[bg-task-result]`, `[agent-reply:<name>]`, `[app:<name>]`. A schedule's did
not, so an agent woken at 07:00 could not tell its own daily run from
something the user had just typed. It now arrives as:

```
[scheduled:<name> — <recurrence>]
This turn was started by a schedule, not by the user. Do the work and report
the result; nobody is waiting to answer questions.

<the schedule's prompt>
```

That last sentence is the point: a scheduled run that ends by asking a
clarifying question has asked nobody, and blocks until the question times out.

## 4. "Last run" opens the run

`last_run_session_id` is also what the UI needed: the Schedules page could say
*when* a schedule last fired but not *what happened*, which is the actual
question. The row is now a link into that session — for throwaway fires, the
auto-archived session the turn ran in.

## 5. What this defers

* **Per-fire history.** One pointer, to the latest run. A real run list means
  a `schedule_runs` table; the sessions themselves already hold the history,
  and nothing yet needs to page through it.
* **Catch-up for missed fires.** A schedule that was due while Octopus was
  down does not fire on boot (one-time schedules are deleted, recurring ones
  wait for the next tick). Firing a backlog on startup is rarely what anyone
  wants, and "run everything I missed" needs a policy per schedule.
* **A skipped-fire signal in the UI.** The skip is logged; surfacing it means
  deciding whether a skipped tick is worth a badge, which needs a real case of
  someone being surprised by one.
