import { useState } from "react";
import { taskPage } from "./job-status";
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
  max_concurrent_jobs?: number;
  effective_limit?: number;
  max_encoding_tasks: number;
  max_crf_tasks?: number;
  max_other_tasks: number;
  max_release_tasks?: number;
  running_release_tasks?: number;
  effective_encoding_limit: number;
  effective_crf_limit?: number;
  effective_other_limit: number;
  running_encoding_tasks: number;
  running_crf_tasks?: number;
  running_other_tasks: number;
  capacity: number;
  paused: boolean;
  running: QueueTask[];
  queued: QueueTask[];
};

export function useQueue() {
  return useQuery({
    queryKey: ["queue"],
    queryFn: () => api<QueueState>("/queue"),
    refetchInterval: 3000,
  });
}

function PoolUsage({ data }: { data: QueueState }) {
  return (
    <>
      Encoding {data.running_encoding_tasks}/{data.effective_encoding_limit}
      {typeof data.max_crf_tasks === "number" && (
        <>
          {" "}
          · CRF {data.running_crf_tasks}/{data.effective_crf_limit}
        </>
      )}
      {" · Other tasks "}
      {data.running_other_tasks}/{data.effective_other_limit}
      {typeof data.max_release_tasks === "number" && (
        <>
          {" "}
          (release {data.running_release_tasks}/{data.max_release_tasks})
        </>
      )}
    </>
  );
}

export function QueueSummary() {
  const { data, error } = useQueue();
  return (
    <section className="queue-summary">
      <div>
        <h2>
          Processing queue{" "}
          {data?.paused && <span className="badge needs-input">Paused</span>}
        </h2>
        {data && (
          <p>
            {typeof data.max_encoding_tasks === "number" ? (
              <>
                <PoolUsage data={data} />
              </>
            ) : (
              <>
                {new Set(data.running.map((task) => task.job_id)).size} /{" "}
                {data.effective_limit} running jobs
              </>
            )}{" "}
            · {data.queued.filter((t) => !t.held).length} queued ·{" "}
            {data.queued.filter((t) => t.held).length} held
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
  const [draftEncoding, setDraftEncoding] = useState<string | null>(null);
  const [draftCrf, setDraftCrf] = useState<string | null>(null);
  const [draftOther, setDraftOther] = useState<string | null>(null);
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
            Set separate limits for encoding, CRF analysis, and other tasks, and
            choose which queued task goes next.
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
                className={`secondary ${data.paused ? "needs-input" : ""}`}
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
            {typeof data.max_encoding_tasks !== "number" ? (
              <p className="callout" role="status">
                The current server uses a shared limit of {data.effective_limit}{" "}
                running jobs. Separate encoding, CRF, and other-task limits
                become available after the server update. Running jobs can
                continue with their current settings.
              </p>
            ) : (
              <>
                <form
                  className="inline-form queue-limit-form"
                  onSubmit={(event) => {
                    event.preventDefault();
                    change.mutate(
                      {
                        path: "/queue",
                        body: {
                          max_encoding_tasks: Number(
                            draftEncoding ?? data.max_encoding_tasks,
                          ),
                          ...(typeof data.max_crf_tasks === "number"
                            ? {
                                max_crf_tasks: Number(
                                  draftCrf ?? data.max_crf_tasks,
                                ),
                              }
                            : {}),
                          max_other_tasks: Number(
                            draftOther ?? data.max_other_tasks,
                          ),
                        },
                      },
                      {
                        onSuccess: () => {
                          setDraftEncoding(null);
                          setDraftCrf(null);
                          setDraftOther(null);
                        },
                      },
                    );
                  }}
                >
                  <label>
                    Maximum encoding tasks
                    <input
                      required
                      type="number"
                      min="1"
                      max={data.capacity}
                      step="1"
                      disabled={change.isPending}
                      value={draftEncoding ?? data.max_encoding_tasks}
                      onChange={(event) => setDraftEncoding(event.target.value)}
                    />
                  </label>
                  {typeof data.max_crf_tasks === "number" && (
                    <label>
                      Maximum CRF analysis tasks
                      <input
                        required
                        type="number"
                        min="1"
                        max={data.capacity}
                        step="1"
                        disabled={change.isPending}
                        value={draftCrf ?? data.max_crf_tasks}
                        onChange={(event) => setDraftCrf(event.target.value)}
                      />
                    </label>
                  )}
                  <label>
                    Maximum other tasks
                    <input
                      required
                      type="number"
                      min="1"
                      max={data.capacity}
                      step="1"
                      disabled={change.isPending}
                      value={draftOther ?? data.max_other_tasks}
                      onChange={(event) => setDraftOther(event.target.value)}
                    />
                  </label>
                  <button disabled={change.isPending}>Apply limits</button>
                  <span>
                    <PoolUsage data={data} />
                  </span>
                </form>
                <p className="muted">
                  {typeof data.max_crf_tasks === "number"
                    ? "Final video encoding and CRF analysis each have their own queue and limit. Source scans, track review, extraction, validation, remuxing, screenshots and release generation share the other-task limit."
                    : "CRF analysis shares other-task slots on this server. Its separate limit becomes available after the server update."}{" "}
                  {typeof data.max_release_tasks === "number" && (
                    <>
                      Release generation runs one task at a time within the
                      other-task limit.{" "}
                    </>
                  )}
                  Limits count tasks across all jobs, including tasks running
                  alongside one another in the same job. At most {data.capacity}{" "}
                  tasks run in total.
                </p>
              </>
            )}
            <p className="muted">
              Pause queue stops new work from starting. Use Pause encoding on a
              running encode to suspend it and retain its progress. Paused
              encodes keep their encoding slots. Lowering a limit lets running
              tasks finish. Higher limits share CPU and memory between jobs.
            </p>
          </section>
          <section>
            <h2>Running tasks</h2>
            {data.running.length === 0 && (
              <p className="muted">No tasks are running.</p>
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
                <span
                  className={`badge ${encodingPauseState(task)?.status === "Paused" ? "needs-input" : "running"}`}
                >
                  {encodingPauseState(task)?.status ??
                    (task.cancel_requested ? "Stopping" : "Running")}
                </span>
                <EncodingPauseButton task={task} />
                <Link to={`/jobs/${task.job_id}/${taskPage(task.type)}`}>
                  View progress →
                </Link>
              </div>
            ))}
          </section>
          <section>
            <div className="section-heading">
              <h2>Queued tasks</h2>
              <span>{data.queued.length} waiting</span>
            </div>
            <p className="muted">
              Ready tasks start in this order when their pool has a free slot. A
              full encoding pool does not block other tasks. Held tasks keep
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
                <span
                  className={`badge ${task.held ? "needs-input" : "queued"}`}
                >
                  {task.held ? "Held" : "Queued"}
                </span>
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
                    className={`secondary ${task.held ? "needs-input" : ""}`}
                    disabled={change.isPending}
                    onClick={() =>
                      change.mutate({
                        path: `/queue/tasks/${task.id}`,
                        body: { held: !task.held },
                      })
                    }
                  >
                    {task.held ? "Resume task" : "Hold task"}
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
