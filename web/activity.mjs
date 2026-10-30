// Project the existing observation journals into one ordered session activity stream.
// Action IDs are scoped to their observation; progress updates keep the same row.
export function sessionActivity(observations) {
  const items = [];
  for (const entry of observations) {
    items.push({id: `preview:${entry.frame_id}`, kind: "preview", entry, elapsed: entry.timestamp_s});
    const actions = new Map();
    let outcome;
    for (const event of entry.events || []) {
      if (event.kind === "run") {
        if (event.status !== "started") outcome = event;
        continue;
      }
      const capture = event.kind === "capture" && event.action_id.endsWith(":capture");
      const toolId = capture ? `tool:${event.action_id.slice(0, -8)}` : null;
      const key = capture && actions.has(toolId) ? toolId : event.action_id;
      let item = actions.get(key);
      if (!item) {
        item = {id: `action:${entry.frame_id}:${key}`, kind: event.kind, entry,
          elapsed: entry.timestamp_s + event.elapsed_s, timestamp: event.timestamp, sequence: event.sequence, event: {}};
        actions.set(key, item);
      }
      if (capture && key === toolId) item.capture = {...item.capture, ...event};
      else item.event = {...item.event, ...event};
      item.revision = event.id;
    }
    const ordered = [...actions.values()];
    const lastModel = ordered.findLast(item => item.kind === "model");
    if (outcome?.outcome === "no_photo" && lastModel) {
      lastModel.noPhoto = true;
      lastModel.revision = outcome.id;
    } else if (outcome && !["downloaded", "preview_interval_set", "preview_interval_unchanged"].includes(outcome.outcome)
        && !outcome.outcome.startsWith("skipped_")
        && !(outcome.outcome === "error" && ordered.some(item => item.event.status === "failed"))) {
      ordered.push({id: `outcome:${entry.frame_id}`, kind: "outcome", entry, event: outcome,
        elapsed: entry.timestamp_s + outcome.elapsed_s, timestamp: outcome.timestamp, sequence: outcome.sequence, revision: outcome.id});
    }
    items.push(...ordered.sort((a, b) => a.sequence - b.sequence));
  }
  return items;
}
