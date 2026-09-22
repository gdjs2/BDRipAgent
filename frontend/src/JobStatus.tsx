import { jobStatus } from "./job-status";
import { useQueue } from "./Queue";
import type { Job } from "./types";

export function JobStatus({ job }: { job: Job }) {
  const { data: queue } = useQueue();
  const status = jobStatus(job, queue?.paused);
  return (
    <div className="job-execution-status" aria-label="Job status">
      <span className={`badge ${status.tone}`}>{status.label}</span>
      {status.detail && <small>{status.detail}</small>}
    </div>
  );
}
