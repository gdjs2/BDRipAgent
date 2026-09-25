import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import { api } from "./api";

type Prompt = {
  key: string;
  title: string;
  description: string;
  text: string;
  default_text: string;
  revision: number;
  customized: boolean;
  updated_at: string | null;
};
type Draft = { text: string; revision: number };
const storageKey = "bdrip-agent-prompt-drafts";
function readDrafts(): Record<string, Draft> {
  try {
    return JSON.parse(sessionStorage.getItem(storageKey) ?? "{}");
  } catch {
    return {};
  }
}

export function AgentPrompts() {
  const query = useQueryClient();
  const [params, setParams] = useSearchParams();
  const [drafts, setDrafts] = useState<Record<string, Draft>>(readDrafts);
  const [notice, setNotice] = useState("");
  const prompts = useQuery({
    queryKey: ["agent-prompts"],
    queryFn: () => api<Prompt[]>("/agent/prompts"),
  });
  const selected =
    prompts.data?.find((item) => item.key === params.get("task")) ??
    prompts.data?.[0];
  const draft = selected ? drafts[selected.key] : undefined;
  const text = draft?.text ?? selected?.text ?? "";
  const changed = !!selected && text !== selected.text;
  const stale = !!draft && draft.revision !== selected?.revision;
  useEffect(() => {
    try {
      sessionStorage.setItem(storageKey, JSON.stringify(drafts));
    } catch {
      /* Editor still works when browser storage is full. */
    }
  }, [drafts]);
  const save = useMutation({
    mutationFn: (item: Prompt) =>
      api<Prompt>(
        `/agent/prompts/${item.key}`,
        {
          revision: drafts[item.key]?.revision ?? item.revision,
          text: text === item.default_text ? null : text,
        },
        "PUT",
      ),
    onSuccess: (saved) => {
      query.setQueryData<Prompt[]>(["agent-prompts"], (items) =>
        items?.map((item) => (item.key === saved.key ? saved : item)),
      );
      setDrafts((current) => {
        const next = { ...current };
        delete next[saved.key];
        return next;
      });
      setNotice(
        `${saved.title} saved · revision ${saved.revision}. New task attempts will use this version.`,
      );
    },
  });
  function edit(value: string) {
    if (!selected) return;
    setNotice("");
    save.reset();
    setDrafts((current) => ({
      ...current,
      [selected.key]: {
        text: value,
        revision: current[selected.key]?.revision ?? selected.revision,
      },
    }));
  }
  function discard() {
    if (!selected) return;
    setDrafts((current) => {
      const next = { ...current };
      delete next[selected.key];
      return next;
    });
    save.reset();
    setNotice("");
  }
  const canSave = changed && !stale && !!text.trim() && !save.isPending;
  return (
    <div className="agent-prompts-page">
      <header className="section-heading">
        <div>
          <div className="eyebrow">AGENT WORKSPACE</div>
          <h1>Agent prompts</h1>
          <p>Shape how the agent reviews, repairs, and selects.</p>
        </div>
        <Link className="button secondary" to="/agent">
          View Agent Live ↗
        </Link>
      </header>
      <p className="prompt-scope">
        These instructions apply to all movies. Each task attempt keeps a
        snapshot, so an active task’s batches stay consistent. Save before
        starting or retrying a task. Completed work is not rerun automatically.
      </p>
      {prompts.isPending && <p role="status">Loading prompts…</p>}
      {prompts.error && (
        <p className="error" role="alert">
          {prompts.error.message}{" "}
          <button onClick={() => prompts.refetch()}>Retry</button>
        </p>
      )}
      {selected && (
        <div className="prompt-workspace">
          <nav className="prompt-task-list" aria-label="Agent task prompts">
            {prompts.data?.map((item) => (
              <button
                key={item.key}
                className={selected.key === item.key ? "selected" : ""}
                aria-pressed={selected.key === item.key}
                disabled={save.isPending}
                onClick={() => {
                  setParams({ task: item.key });
                  save.reset();
                  setNotice("");
                }}
              >
                <span>{item.title}</span>
                <small>
                  {drafts[item.key] && drafts[item.key].text !== item.text
                    ? "Unsaved draft"
                    : item.customized
                      ? "Customized"
                      : "Default"}
                </small>
              </button>
            ))}
          </nav>
          <section className="prompt-editor">
            <div className="section-heading">
              <div>
                <h2>{selected.title}</h2>
                <p>{selected.description}</p>
              </div>
              <span
                className={`badge ${changed ? "needs-input" : "succeeded"}`}
              >
                {changed
                  ? "Unsaved"
                  : selected.customized
                    ? "Customized"
                    : "Default"}
              </span>
            </div>
            <label htmlFor="agent-prompt-text">
              Instructions sent to the agent
            </label>
            <textarea
              id="agent-prompt-text"
              value={text}
              spellCheck={false}
              maxLength={50000}
              disabled={save.isPending}
              onChange={(event) => edit(event.target.value)}
              onKeyDown={(event) => {
                if (
                  (event.ctrlKey || event.metaKey) &&
                  event.key.toLowerCase() === "s"
                ) {
                  event.preventDefault();
                  if (canSave) save.mutate(selected);
                }
              }}
              aria-describedby="agent-prompt-context"
            />
            <div className="prompt-editor-meta">
              <span>{text.length.toLocaleString()} / 50,000 characters</span>
              <span>
                Saved revision {selected.revision}
                {selected.updated_at
                  ? ` · ${new Date(selected.updated_at).toLocaleString()}`
                  : " · Built-in instructions"}
              </span>
            </div>
            <p className="muted" id="agent-prompt-context">
              Movie evidence, cue IDs, images, task parameters, and the required
              JSON output format are attached automatically. Use plain
              instructions here; no template variables are needed. Agent Live
              shows the complete prompt actually sent.
            </p>
            {(stale || save.error) && (
              <div className="callout needs-input" role="alert">
                <p>
                  {save.error?.message ??
                    "A newer version was saved in another window. Your draft has been kept."}
                </p>
                <button
                  className="secondary"
                  disabled={prompts.isFetching}
                  onClick={async () => {
                    const result = await prompts.refetch();
                    if (!result.isError) discard();
                  }}
                >
                  Load saved version
                </button>
              </div>
            )}
            <div className="prompt-editor-actions">
              <button disabled={!canSave} onClick={() => save.mutate(selected)}>
                {save.isPending
                  ? "Saving…"
                  : text === selected.default_text && selected.customized
                    ? "Save default prompt"
                    : "Save prompt"}
              </button>
              <button
                className="secondary"
                disabled={!changed || save.isPending}
                onClick={discard}
              >
                Discard edits
              </button>
              <button
                className="text-button"
                disabled={text === selected.default_text || save.isPending}
                onClick={() => edit(selected.default_text)}
              >
                Restore default
              </button>
              <small className="muted">Ctrl / ⌘ + S to save</small>
            </div>
            {notice && (
              <p className="success" role="status">
                {notice}
              </p>
            )}
            <details className="prompt-default">
              <summary>Read the built-in prompt</summary>
              <pre>{selected.default_text}</pre>
            </details>
            <small className="muted">
              Unsaved drafts stay in this browser tab when you switch tasks or
              pages.
            </small>
          </section>
        </div>
      )}
    </div>
  );
}
