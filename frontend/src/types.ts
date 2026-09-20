export type Profile = {
  codec: string;
  encoder: string;
  preset: string;
  bit_depth: number;
  tune?: string | null;
  video_profile?: string | null;
  level?: string | number | null;
  extra_options?: string | null;
  crf_min?: number;
  crf_max?: number;
};
export type Policy = {
  decoder?: "cpu" | "cuda";
  count: number;
  representative: number;
  encode_challenging: number;
  min_spacing_seconds: number;
  min_timeline_bins: number;
  max_per_scene: number;
  policy: string;
};
export type Config = {
  profiles: Record<string, Profile>;
  screenshots: Policy;
  stages: string[];
  release?: { upload_host: string; upload_configured: boolean };
};
export type TrackFlag =
  "default" | "forced" | "hearing_impaired" | "visual_impaired" | "commentary";
export type Track = {
  track_id: number;
  kind: string;
  info: {
    codec: string;
    codec_id: string;
    language: string;
    name: string;
    mux_name?: string;
    name_override?: string;
    suggested_name?: string;
    base_name?: string;
    source_subtitle_metadata?: { language: string; name: string };
    hearing_impaired?: boolean | null;
    subtitle_detection?: {
      schema_version: number;
      status?: "resolved" | "inconclusive";
      language_confident?: boolean;
      sdh_confident?: boolean;
      hearing_impaired?: boolean | null;
      method: string;
      sampled_cues: number;
      unique_cues: number;
      explanation: string;
    };
    channels?: number;
    channel_layout?: string;
    sample_rate?: number;
    bit_depth?: number;
    bitrate?: string;
    default: boolean | null;
    forced: boolean | null;
    commentary: boolean | null;
    visual_impaired?: boolean | null;
    flag_overrides?: Partial<Record<TrackFlag, boolean>>;
    track_review?: {
      schema_version: number;
      description: string;
      flag_explanation: string;
      confidence: string;
      flags: Record<TrackFlag, boolean | null>;
    };
    audio_analysis?: {
      method: string;
      sampled_seconds?: number;
      source_duration_seconds?: number;
      transcription_available?: boolean;
      limitations: string[];
      samples: {
        id: number;
        start_seconds: number;
        duration_seconds: number;
        detected_language?: string;
        language_probability?: number;
        segments?: { start: number; end: number; text: string }[];
      }[];
    };
    extractable: boolean;
  };
};
export type Task = {
  id: string;
  type: string;
  stage: string;
  status: string;
  progress: number;
  progress_detail: Record<string, number | string | boolean | null>;
  command_json?: string[][];
  error_message?: string;
  created_at: string;
  started_at?: string | null;
  finished_at?: string | null;
  held?: boolean;
  cancel_requested?: boolean;
  can_pause?: boolean;
  pause_requested?: boolean;
  paused_at?: string | null;
  attempt: number;
};
export type Artifact = {
  id: string;
  path: string;
  artifact_type: string;
  size: number;
  info: { candidate_id?: number };
};
export type Point = {
  crf: number;
  bitrate_kbps: number;
  average_qp: number | null;
};
export type RateControl = "crf" | "bitrate";
export type IMDbMovie = {
  imdb_id: string;
  title: string;
  year: number;
  url: string;
  title_options: { title: string; filenames: Record<string, string> }[];
};
export type Job = {
  id: string;
  title: string;
  source_path: string;
  release_name: string;
  year?: number;
  imdb_id?: string;
  imdb_metadata?: IMDbMovie;
  state: string;
  created_at: string;
  analysis_profile: string;
  tracks: Track[];
  tasks: Task[];
  artifacts: Artifact[];
  screenshot_policy: Policy;
  analysis: {
    track_review_version?: number;
    release_details?: ReleaseDetails;
    release_result?: ReleaseResult;
    smoke_test?: boolean;
    video?: {
      codec: string;
      width: number;
      height: number;
      fps: string;
      bit_depth: number;
      duration: number;
    };
    crop?: Record<string, number>;
  };
  validation: {
    valid?: boolean | null;
    skipped?: boolean;
    smoke_test?: boolean;
    errors?: string[];
    warnings?: string[];
    metrics?: Record<string, unknown>;
  };
  crf?: {
    data: {
      samples: Point[];
      predicted: Point[];
      profile_snapshot?: Profile;
      statistics: Record<string, unknown>;
    };
  };
  track_selection?: { audio_track_ids: number[]; subtitle_track_ids: number[] };
  encode_config?: {
    data: {
      codec: string;
      profile: string;
      execution_mode?: "smoke";
      rate_control?: RateControl;
      crf?: number | null;
      bitrate_kbps?: number | null;
      profile_snapshot: Profile;
      selected_by?: string;
      selected_at?: string;
    };
  };
};
export type ReleaseDetails = {
  upload_screenshots: boolean;
  source: string;
  chinese_name: string;
  extra_description: string;
  tracker: string;
};
export type ReleaseResult = {
  task_id: string;
  infohash: string;
  md5: string;
  uploaded_images: number;
  package_path: string;
  package_storage?: string;
  torrent_path?: string;
  torrent_storage?: string;
  bundle_path?: string;
  upload_screenshots?: boolean;
  warnings: string[];
  artifacts: { path: string; kind: string; storage: string }[];
};
export type Shot = {
  reservation?: {
    job_id: string;
    codec: string;
    frame_number: number;
    spacing_seconds: number;
  } | null;
  id: string;
  candidate_id: number;
  selected: boolean;
  shortlisted: boolean;
  info: {
    source_frame_number: number;
    source_pts_seconds: number;
    timeline_seconds: number;
    category?: string;
    reason?: string;
    path: string;
    picture_type?: string;
    encoded_picture_type?: string;
    b_frames_verified?: boolean;
    recommendation_rank?: number | null;
    thumbnail?: string;
    review_comparisons?: { src: string; encode: string };
    comparisons?: { src: string; encode: string };
  };
};
