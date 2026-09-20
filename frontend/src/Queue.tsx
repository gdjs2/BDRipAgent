import { useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { EncodingPauseButton } from "./EncodingPauseButton";
import { encodingPauseState } from "./encoding-pause";
import { api, readable } from "./api";

type QueueTask = {
  id: string;
  job_id: string;
  title: string;
  profile: string;
  smoke_test?: boolean;
  type: string;
  status: string;
  held: boolean;
  progress: number;
  cancel_requested: boolean;
  can_pause?: boolean;
  pause_requested?: boolean;
  paused_at?: string | null;
};
type QueueState = {
  max_concurrent_jobs: number;
  effective_limit: number;
  capacity: number;
  paused: boolean;
  running: QueueTask[];
  queued: QueueTask[];
};

function useQueue() {
  return useQuery({
    queryKey: ["queue"],
    queryFn: () => api<QueueState>("/queue"),
    refetchInterval: 3000,
  });
}

export function QueueSummary() {
  const { data, error } = useQueue();
  return (
    <section className="queue-summary">
      <div>
        <h2>
          Processing queue{" "}
          {data?.paused && <span className="badge">Paused</span>}
        </h2>
        {data && (
          <p>
            {data.running.length} running ·{" "}
            {data.queued.filter((t) => !t.held).length} queued ·{" "}
            {data.queued.filter((t) => t.held).length} held · limit{" "}
            {data.effective_limit}
          </p>
        )}
        {error && <p className="error">{error.message}</p>}
      </div>
      <Link className="button secondary" to="/queue">
        Manage queue →
      </Link>
    </section>
  );
}

export function QueuePage() {
  const query = useQueryClient();
  const { data, error } = useQueue();
  const [draftLimit, setDraftLimit] = useState<string | null>(null);
  const change = useMutation({
    mutationFn: ({
      path,
      body,
      method = "PATCH",
    }: {
      path: string;
      body: unknown;
      method?: string;
    }) => api<QueueState>(path, body, method),
    onSuccess: (result) => {
      query.setQueryData(["queue"], result);
      query.invalidateQueries({ queryKey: ["jobs"] });
      query.invalidateQueries({ queryKey: ["job"] });
    },
  });
  const move = (index: number, offset: number) => {
    const ids = data!.queued.map((t) => t.id);
    [ids[index], ids[index + offset]] = [ids[index + offset], ids[index]];
    change.mutate({
      path: "/queue/order",
      body: { task_ids: ids },
      method: "PUT",
    });
  };
  return (
    <>
      <header>
        <div>
          <div className="eyebrow">WORK SCHEDULING</div>
          <h1>Processing queue</h1>
          <p>
            Choose how many jobs run at once and which queued job goes next.
          </p>
        </div>
      </header>
      {(error || change.error) && (
        <div className="error" role="alert">
          {(error || change.error)?.message}
        </div>
      )}
      {data && (
        <>
          <section>
            <div className="section-heading">
              <h2>{data.paused ? "Queue paused" : "Queue running"}</h2>
              <button
                className="secondary"
                disabled={change.isPending}
                onClick={() =>
                  change.mutate({
                    path: "/queue",
                    body: { paused: !data.paused },
                  })
                }
              >
                {data.paused ? "Resume queue" : "Pause queue"}
              </button>
            </div>
            <form
              className="inline-form"
              onSubmit={(event) => {
                event.preventDefault();
                change.mutate(
                  {
                    path: "/queue",
                    body: {
                      max_concurrent_jobs: Number(
                        draftLimit ?? data.max_concurrent_jobs,
                      ),
                    },
                  },
                  { onSuccess: () => setDraftLimit(null) },
                );
              }}
            >
              <label>
                Maximum simultaneous jobs
                <input
                  required
                  type="number"
                  min="1"
                  max={data.capacity}
                  step="1"
                  value={draftLimit ?? data.max_concurrent_jobs}
                  onChange={(event) => setDraftLimit(event.target.value)}
                />
              </label>
              <button disabled={change.isPending}>Apply limit</button>
              <span>
                {data.running.length} running / {data.effective_limit} slots
              </span>
            </form>
            <p className="muted">
              The limit applies to automatic stages, including analysis,
              encoding and validation. Jobs waiting for your track or CRF
              decision use no slot.
            </p>
            <p className="muted">
              Pause queue stops new work from starting. Use Pause encoding on a
              running encode to suspend it and retain its progress. Paused
              encodes keep their slots. Higher limits share CPU and memory
              between jobs.
            </p>
          </section>
          <section>
            <h2>Running jobs</h2>
            {data.running.length === 0 && (
              <p className="muted">No jobs are running.</p>
            )}
            {data.running.map((task) => (
              <div className="queue-row" key={task.id}>
                <Link className="grow" to={`/jobs/${task.job_id}`}>
                  <h3>{task.title}</h3>
                  <span className="muted">
                    {task.profile} · {readable(task.type)}
                    {task.smoke_test && " · Smoke test"}
                  </span>
                </Link>
                <span className="badge">
                  {encodingPauseState(task)?.status ??
                    (task.cancel_requested ? "Stopping" : "Running")}
                </span>
                <EncodingPauseButton task={task} />
                <Link
                  to={`/jobs/${task.job_id}/${task.type === "crf_analysis" ? "crf" : "encode"}`}
                >
                  View progress →
                </Link>
              </div>
            ))}
          </section>
          <section>
            <div className="section-heading">
              <h2>Queued jobs</h2>
              <span>{data.queued.length} waiting</span>
            </div>
            <p className="muted">
              Ready jobs start in this order when a slot is free. Held jobs keep
              their place and are skipped until resumed.
            </p>
            {data.queued.length === 0 && (
              <p className="muted">
                Nothing queued. Confirm a CRF in a job to add its encode.
              </p>
            )}
            {data.queued.map((task, index) => (
              <div
                className="queue-row"
                key={task.id}
                data-testid={`queue-task-${task.id}`}
              >
                <span className="queue-position">{index + 1}</span>
                <Link className="grow" to={`/jobs/${task.job_id}`}>
                  <h3>{task.title}</h3>
                  <span className="muted">
                    {task.profile} · {readable(task.type)}
                    {task.smoke_test && " · Smoke test"}
                  </span>
                </Link>
                <span className="badge">{task.held ? "Held" : "Queued"}</span>
                <div className="queue-actions">
                  <button
                    className="secondary"
                    disabled={change.isPending || index === 0}
                    aria-label={`Move ${task.title} up`}
                    onClick={() => move(index, -1)}
                  >
                    ↑
                  </button>
                  <button
                    className="secondary"
                    disabled={
                      change.isPending || index === data.queued.length - 1
                    }
                    aria-label={`Move ${task.title} down`}
                    onClick={() => move(index, 1)}
                  >
                    ↓
                  </button>
                  <button
                    className="secondary"
                    disabled={change.isPending}
                    onClick={() =>
                      change.mutate({
                        path: `/queue/tasks/${task.id}`,
                        body: { held: !task.held },
                      })
                    }
                  >
                    {task.held ? "Resume job" : "Hold job"}
                  </button>
                </div>
              </div>
            ))}
          </section>
        </>
      )}
    </>
  );
}
