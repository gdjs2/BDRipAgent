export type Profile = {
  codec: string;
  encoder: string;
  preset: string;
  bit_depth: number;
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
export type Track = {
  track_id: number;
  kind: string;
  info: {
    codec: string;
    codec_id: string;
    language: string;
    name: string;
    mux_name?: string;
    hearing_impaired?: boolean;
    channels?: number;
    channel_layout?: string;
    sample_rate?: number;
    bit_depth?: number;
    bitrate?: string;
    default: boolean;
    forced: boolean;
    commentary: boolean;
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
  error_message?: string;
  created_at: string;
  started_at?: string | null;
  finished_at?: string | null;
  held?: boolean;
  cancel_requested?: boolean;
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
    };
  };
};
export type ReleaseDetails = {
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
  warnings: string[];
  artifacts: { path: string; kind: string; storage: string }[];
};
export type Shot = {
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
