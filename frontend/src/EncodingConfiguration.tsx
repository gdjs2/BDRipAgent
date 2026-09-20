import {
  displayCommand,
  outputDimensions,
  recordedEncodingCommand,
} from "./encoding-configuration";
import type { Job } from "./types";

export function EncodingConfiguration({ job }: { job: Job }) {
  const config = job.encode_config?.data;
  if (!config)
    return (
      <section>
        <h2>Encoding configuration</h2>
        <p className="muted">
          Choose the encoding target in CRF analysis to save the full
          configuration here.
        </p>
      </section>
    );
  const profile = config.profile_snapshot;
  const smoke = config.execution_mode === "smoke";
  const bitrate = config.rate_control === "bitrate";
  const video = job.analysis.video;
  const crop = job.analysis.crop;
  const dimensions = outputDimensions(job);
  const { task, argv } = recordedEncodingCommand(job);
  const rows: [string, string | number][] = [
    ["Profile", config.profile],
    ["Codec / encoder", `${profile.codec} / ${profile.encoder}`],
    ["Bit depth", `${profile.bit_depth}-bit`],
    ["Preset", profile.preset],
    ["Tune", profile.tune || "None"],
    ["Video profile", profile.video_profile || "Automatic"],
    ["Level", profile.level ?? "Automatic"],
    [
      "Rate control",
      smoke
        ? "Skipped — source video reused"
        : bitrate
          ? "Average bitrate (2-pass)"
          : "Constant quality (CRF)",
    ],
    ...(!smoke
      ? bitrate
        ? ([
            [
              "Target video bitrate",
              `${config.bitrate_kbps?.toLocaleString()} kbps (${((config.bitrate_kbps ?? 0) / 1000).toFixed(3)} Mbps)`,
            ],
            ["Passes", "2 · full first pass (turbo disabled)"],
          ] as [string, string][])
        : ([
            ["CRF", config.crf ?? "Not recorded"],
            ["Passes", 1],
          ] as [string, string | number][])
      : []),
    ["Source", job.source_path],
    [
      "Source video",
      video
        ? `${video.width} × ${video.height} · ${video.codec} · ${video.bit_depth}-bit`
        : "Not recorded",
    ],
    ["Source frame rate", video?.fps ?? "Not recorded"],
    [
      "Output dimensions",
      dimensions
        ? `${dimensions.width} × ${dimensions.height}`
        : "Not recorded",
    ],
    [
      "Crop (top / bottom / left / right)",
      smoke
        ? "Not applied — source video reused"
        : crop
          ? `${crop.top} / ${crop.bottom} / ${crop.left} / ${crop.right} pixels`
          : "Not recorded",
    ],
    ...(!smoke
      ? ([
          ["Frame timing", "Source timing (VFR)"],
          ["Pixel aspect ratio", "Square pixels (non-anamorphic)"],
          [
            "Filters",
            "Comb detection, deinterlace, decomb, detelecine, denoise and sharpening disabled",
          ],
          ["Encoding container", "Matroska · video only"],
        ] as [string, string][])
      : []),
    [
      "Audio / subtitles",
      "Selected original audio and processed subtitles are added during muxing",
    ],
    ["Chapters", "Source chapters are added during muxing"],
    ["Selected by", config.selected_by ?? "Not recorded"],
    [
      "Selected at",
      config.selected_at
        ? new Date(config.selected_at).toLocaleString()
        : "Not recorded",
    ],
  ];
  return (
    <section className="encoding-configuration">
      <h2>Encoding configuration</h2>
      <p className="muted">
        Saved for this job when you confirmed the encoding target.{" "}
        {smoke &&
          "Smoke mode skips HandBrake; the saved encoder profile is shown for reference."}
      </p>
      <table>
        <tbody>
          {rows.map(([label, value]) => (
            <tr key={label}>
              <th scope="row">{label}</th>
              <td>{value}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <h3>Additional encoder options</h3>
      <pre>{profile.extra_options || "None"}</pre>
      <h3>
        HandBrake command{argv && task ? ` · attempt ${task.attempt}` : ""}
      </h3>
      {smoke ? (
        <p className="muted">No encoding command runs in smoke mode.</p>
      ) : argv ? (
        <pre>{displayCommand(argv)}</pre>
      ) : (
        <p className="muted">
          The exact command will appear when the encoder starts
          {task ? ` attempt ${task.attempt}` : ""}.
        </p>
      )}
      <details>
        <summary>Complete saved configuration (JSON)</summary>
        <pre>
          {JSON.stringify(
            {
              ...config,
              source_path: job.source_path,
              source_video: job.analysis.video,
              crop: job.analysis.crop,
              selected_tracks: job.track_selection,
            },
            null,
            2,
          )}
        </pre>
      </details>
    </section>
  );
}
