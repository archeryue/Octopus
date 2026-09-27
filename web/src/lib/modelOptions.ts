/**
 * What `/model` offers.
 *
 * Three sources, in this order, because each answers a different question:
 *
 *  1. **The agent's default** — the way back. Choosing it clears the session's
 *     override rather than storing a name, so the session keeps following the
 *     agent if the agent is later re-pointed.
 *  2. **The backend's shortlist** (`GET /api/backends` → `models`) — the names
 *     that harness's CLI is known to take. A shortlist, not a whitelist.
 *  3. **Models already in use** by this user's own agents and sessions — real
 *     data, so a model this build has never heard of still shows up once it has
 *     been used, and Codex (whose shortlist is deliberately empty) is not an
 *     empty menu forever.
 *
 * Anything not listed is still reachable as `/model <name>`, which is why the
 * server stores whatever string it is given.
 */

export interface ModelOption {
  /** null = "use the agent's model", i.e. clear the override. */
  value: string | null;
  label: string;
  /** Where it came from, for the hint the row shows. */
  source: "agent" | "backend" | "used";
  /** Currently in force for this session. */
  current: boolean;
}

export function buildModelOptions(args: {
  /** The session's own override, or null when it inherits. */
  sessionModel: string | null | undefined;
  /** The owning agent's model, or null when the agent takes the default too. */
  agentModel: string | null | undefined;
  /** Shortlist for the session's backend. */
  backendModels: string[] | undefined;
  /** Every model any of this user's agents or sessions already uses. */
  inUse?: (string | null | undefined)[];
}): ModelOption[] {
  const { sessionModel, agentModel, backendModels, inUse = [] } = args;
  const session = sessionModel || null;
  const agent = agentModel || null;

  const options: ModelOption[] = [
    {
      value: null,
      label: agent ? `Agent default (${agent})` : "Agent default",
      source: "agent",
      current: session === null,
    },
  ];
  const seen = new Set<string>();
  const add = (name: string | null | undefined, source: "backend" | "used") => {
    const model = (name || "").trim();
    if (!model || seen.has(model)) return;
    seen.add(model);
    options.push({
      value: model,
      label: model,
      source,
      current: session === model,
    });
  };
  for (const m of backendModels || []) add(m, "backend");
  for (const m of inUse) add(m, "used");
  // The session's own model may be none of the above — someone typed it.
  add(session, "used");
  return options;
}
