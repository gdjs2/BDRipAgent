export type AgentEvent = {
  type: string;
  invocation_id?: string;
  stage?: string;
  text?: string;
  item_id?: string;
  images?: string[];
};
export type AgentInvocation = {
  id: string;
  stage: string;
  prompt: string;
  images: string[];
  status: string;
  messages: { id: string; text: string }[];
};

export function applyAgentEvent(
  runs: AgentInvocation[],
  event: AgentEvent,
): AgentInvocation[] {
  if (!event.invocation_id) return runs;
  const previous = runs.find((run) => run.id === event.invocation_id);
  const run: AgentInvocation = previous
    ? { ...previous, messages: [...previous.messages] }
    : {
        id: event.invocation_id,
        stage: event.stage ?? "selection",
        prompt: "",
        images: [],
        status: "Running",
        messages: [],
      };
  if (event.type === "prompt") {
    run.prompt = event.text ?? "";
    run.images = event.images ?? [];
  } else if (
    (event.type === "delta" || event.type === "message") &&
    event.item_id
  ) {
    const index = run.messages.findIndex((item) => item.id === event.item_id);
    const message = {
      id: event.item_id,
      text:
        event.type === "delta"
          ? (run.messages[index]?.text ?? "") + (event.text ?? "")
          : (event.text ?? ""),
    };
    if (index === -1) run.messages.push(message);
    else run.messages[index] = message;
  } else if (["status", "complete", "error"].includes(event.type)) {
    run.status = event.text ?? event.type;
  }
  return previous
    ? runs.map((item) => (item.id === run.id ? run : item))
    : [...runs, run];
}
