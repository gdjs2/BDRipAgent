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
  best_count?: number;
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
  audio_review?: { max_rounds: number };
  profiles: Record<string, Profile>;
  screenshots: Policy;
  stages: string[];
  release?: { upload_host: string; upload_configured: boolean };
};
export type DiscoveredSubtitle = {
  requires_attention?: boolean;
  critical_errors?: string[];
  origin?: "upload" | "discovery";
  track_id?: number;
  source_url: string | null;
  download_url: string | null;
  source_filename: string;
  release: string;
  selection_reason: string;
  converter: string;
  cleanup?: {
    single_language: boolean;
    language: string;
    reviewed_cues: number;
    retained_cues: number;
    edited_cues: number;
    reviews: {
      explanation: string;
      edits: {
        cue_id: number;
        original_text: string;
        replacement_text: string | null;
        reason: string;
        action: string;
      }[];
    }[];
  };
  crop_checked: boolean;
  fetched_at: string;
  review: { explanation: string; issues: string[] };
  quality: { cues: number; warnings: string[] };
  alignment: {
    scale: number;
    offset_seconds: number;
    max_error_seconds: number;
    anchor_count: number;
    reference_track_id: number;
    movie_fps: string;
    inferred_candidate_fps: number;
    anchors: {
      candidate_id: number;
      reference_id: string;
      explanation: string;
    }[];
  };
};
export type SubtitleDiscovery = {
  removal?: { allowed: boolean; reason?: string | null };
  allowed: boolean;
  review_required?: boolean;
  policy: { enabled: boolean; original_languages: string[] };
  missing: string[];
  original_language_unknown: boolean;
  active_task?: {
    id: string;
    type?: string;
    job_id: string;
    status: string;
    progress: number;
    detail: { phase?: string };
  } | null;
  report?: {
    status: string;
    summary: string;
    original_languages: string[];
    original_language_sources: string[];
    source_job_id: string;
    missing?: string[];
    needs_review?: boolean;
    reused?: boolean;
    added_tracks: DiscoveredSubtitle[];
    candidates: {
      language: string;
      source_url: string;
      reason: string;
      status: string;
      issues: string[];
      track_id?: number;
    }[];
  } | null;
};
export type TrackFlag =
  "default" | "forced" | "hearing_impaired" | "visual_impaired" | "commentary";
export type Track = {
  track_id: number;
  kind: string;
  info: {
    source_order?: number;
    origin?: "upload" | "discovery";
    discovery?: DiscoveredSubtitle;
    original_filename?: string;
    codec: string;
    codec_id: string;
    language: string;
    language_override?: string;
    language_name?: string;
    name: string;
    mux_name?: string;
    name_override?: string;
    suggested_name?: string;
    base_name?: string;
    source_subtitle_metadata?: { language: string; name: string };
    hearing_impaired?: boolean | null;
    subtitle_detection?: {
      schema_version: number;
      language_code?: string;
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
  lane?: "pipeline" | "tracks";
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
  info: {
    candidate_id?: number;
    backup?: { replaced_at: string; expires_at: string | null };
  };
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
export type AudioComparison = {
  summary: string;
  status: "sampling" | "resolved" | "inconclusive";
  rounds: number;
  max_rounds?: number;
  requires_human?: boolean;
  question?: string;
  stop_reason?: string;
  distinctions: {
    track_id: number;
    compared_with: number[];
    difference: string;
    evidence_sample_ids: number[];
    resolved: boolean;
  }[];
};
export type Job = {
  shared_track_selection?: {
    revision: number;
    applied_revision: number;
    source_job_id: string;
    updated_at: string;
    pending: boolean;
    error?: string | null;
  } | null;
  tracks_editable?: boolean;
  reencode?: { available: boolean; reason?: string | null };
  remux?: { available: boolean; reason?: string | null; backup_days: number };
  track_analysis_complete?: boolean;
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
  subtitle_discovery?: SubtitleDiscovery;
  subtitle_uploads?: {
    id: string;
    filename: string;
    language: string;
    task_id: string;
    status: string;
    detail?: { phase?: string };
    error?: string;
  }[];
  tracks: Track[];
  tasks: Task[];
  artifacts: Artifact[];
  screenshot_policy: Policy;
  analysis: {
    track_review_version?: number;
    remux_revision?: { id: string; requested_at: string };
    audio_comparison?: AudioComparison;
    audio_review_policy?: { max_rounds: number };
    release_details?: ReleaseDetails;
    shared_release_details?: {
      source_job_id: string;
      title: string;
      revision?: number;
      updated_at?: string;
      source_job_available?: boolean;
      snapshot_differs?: boolean;
    };
    shared_track_analysis?: { source_job_id: string; reused: boolean };
    release_result?: ReleaseResult;
    smoke_test?: boolean;
    video?: {
      bit_rate?: number | null;
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
  movie_description: string;
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
