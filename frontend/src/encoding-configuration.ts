import type { Job } from "./types";

export function recordedEncodingCommand(job: Job) {
  const task = job.tasks
    .filter((item) => item.type === "encode")
    .sort((a, b) => b.created_at.localeCompare(a.created_at))[0];
  // A retry with no command yet must not display an older attempt as its command.
  const argv = task?.command_json?.find(
    (args) => args.includes("--encoder-preset") && args.includes("-e"),
  );
  return { task, argv };
}

export function displayCommand(argv: string[]) {
  return argv
    .map((value) =>
      /^[A-Za-z0-9_./:=,+%-]+$/.test(value)
        ? value
        : `'${value.replaceAll("'", "'\\''")}'`,
    )
    .join(" \\\n  ");
}

export function outputDimensions(job: Job) {
  const video = job.analysis.video;
  if (!video) return null;
  if (job.encode_config?.data.execution_mode === "smoke")
    return { width: video.width, height: video.height };
  const crop = job.analysis.crop;
  if (
    !crop ||
    ["top", "bottom", "left", "right"].some(
      (edge) => typeof crop[edge] !== "number",
    )
  )
    return null;
  const width = video.width - crop.left - crop.right;
  const height = video.height - crop.top - crop.bottom;
  return width > 0 && height > 0 ? { width, height } : null;
}
