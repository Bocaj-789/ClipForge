export interface Job {
  id: string;
  job_type: string;
  lane: string;
  priority: number;
  status: string;
  progress_percent: number;
  stage: string | null;
  attempt_count: number;
  max_attempts: number;
  next_attempt_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  error_code: string | null;
  safe_error_message: string | null;
  candidate_video_id: string | null;
  source_id: string | null;
  cancel_requested: boolean;
  created_at: string;
  updated_at: string;
  output: Record<string, unknown>;
}

export interface Candidate {
  id: string;
  origin: string;
  canonical_url: string | null;
  platform: string;
  title: string | null;
  creator_name: string | null;
  duration_seconds: number | null;
  thumbnail_url: string | null;
  discovered_at: string;
  priority: number;
  status: string;
  rights_status: string;
  rights_note: string | null;
  user_note: string | null;
  transcript_status: string;
  has_media: boolean;
  media_is_user_original: boolean;
  blocked_reason: string | null;
  last_error: string | null;
  source_id: string | null;
  media: { width: number; height: number; orientation: string; fps: number } | null;
  latest_job: Job | null;
}

export interface Proposal {
  id: string;
  candidate_video_id: string;
  start_seconds: number;
  end_seconds: number;
  duration_seconds: number;
  scores: Record<string, number>;
  penalties: Record<string, number>;
  final_score: number;
  score_explanation: string;
  warnings: string[];
  provider: string;
  transcript_excerpt: string;
  title_suggestion: string | null;
  crop_offset: number | null;
  status: string;
}

export interface RenderedClip {
  id: string;
  title_suggestion: string;
  description_suggestion: string;
  hashtags: string[];
  render_profile: string;
  caption_style: string;
  crop_mode: string;
  duration_seconds: number;
  width: number;
  height: number;
  file_size_bytes: number;
  warnings: string[];
  review_status: string;
  render_seconds: number | null;
  created_at: string;
  video_url: string;
  thumbnail_url: string;
  captions_url: string;
  proposal?: Proposal;
  source?: {
    id: string;
    title: string | null;
    creator_name: string | null;
    rights_status: string;
    canonical_url: string | null;
  };
}

export interface AgentStatus {
  state: "running" | "paused" | "stopped" | "draining" | "degraded" | "error";
  desired_state: string;
  reason: string | null;
  worker_alive: boolean;
  running_jobs: number;
  degraded: Record<string, string>;
}

export interface Storage {
  used_bytes: number;
  budget_bytes: number;
  used_percent: number;
  disk_free_bytes: number;
  level: "ok" | "warn" | "critical";
  reason: string | null;
  breakdown: Record<string, number>;
}

export interface Summary {
  agent: AgentStatus;
  jobs: Record<string, number>;
  clips_awaiting_review: number;
  rendered_last_24h: number;
  candidates_total: number;
  recent_errors: Job[];
  storage: Storage;
  transcription: { available: boolean; reason: string | null; model: string; downloaded: boolean };
  llm_provider: string;
  sources: {
    id: string;
    name: string;
    kind: string;
    enabled: boolean;
    paused: boolean;
    last_success_at: string | null;
    last_error: string | null;
  }[];
  channels: number;
  recent_publications: { id: string; status: string; created_at: string }[];
  auto_publish_global_enabled: boolean;
  youtube_api_key_configured: boolean;
}

export interface Source {
  id: string;
  kind: string;
  name: string;
  url: string | null;
  enabled: boolean;
  paused: boolean;
  polling_interval_minutes: number;
  category: string | null;
  keywords: string[];
  rights_status: string;
  next_check_at: string | null;
  last_checked_at: string | null;
  last_success_at: string | null;
  last_error: string | null;
  candidates: number;
}

export interface Channel {
  id: string;
  display_name: string;
  enabled: boolean;
  paused: boolean;
  auto_publish_enabled: boolean;
  max_uploads_per_day: number;
  min_spacing_minutes: number;
  timezone: string;
  schedule: { days?: number[]; windows?: [string, string][] };
  default_visibility: string;
  render_profile: string;
  caption_style: string;
  oauth_connected: boolean;
  next_slot: string | null;
  last_upload_at: string | null;
}

export interface Paged<T> {
  items: T[];
  total: number;
  limit?: number;
  offset?: number;
}

export interface Me {
  authenticated: boolean;
  setup_required: boolean;
  username?: string;
  csrf_token?: string;
}
