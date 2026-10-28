/** Shapes from patchtrail/api.py (public_run, list_tasks, health). Optional fields are newer or conditional. */

export type RunState =
  | "queued"
  | "diagnosing"
  | "awaiting_approval"
  | "verifying"
  | "succeeded"
  | "failed"
  | "cancelled"
  | "interrupted";

export interface Health {
  status: string;
  version: string;
  model_configured: boolean;
  model: string;
  operator_configured: boolean;
  container_available: boolean;
  public_demo: boolean;
  demo_mode?: string;
  scope?: string;
  runtime_code_hash?: string;
  /** Only scripts/design_harness.py sets this. */
  design_harness?: string;
}

export interface Task {
  id: string;
  title: string;
  description: string;
  editable_files: string[];
  check_id: string;
  tags: string[];
  source_hash: string;
  private?: boolean;
  project_id?: string;
  snapshot_id?: string;
  check_profile?: string;
  test_paths?: string[];
}

export interface Proposal {
  digest: string;
  path?: string;
  diff: string;
  old_sha256?: string;
  new_sha256?: string;
  base_workspace_hash: string;
  source_hash: string;
  task_id?: string;
  explanation?: string;
  schema_version?: number;
  files?: { path: string; old_sha256: string; new_sha256: string; diff?: string }[];
}

export interface CheckResultData {
  exit_code: number | null;
  output: string;
  duration_ms: number;
  timed_out: boolean;
  truncated: boolean;
  runner: string;
  passed?: boolean;
  tests_run?: number;
  complete?: boolean;
  framework?: string;
  test_outcomes?: { name: string; state: string }[];
}

export interface Run {
  id: string;
  task_id: string;
  mode: "demo" | "live";
  state: RunState;
  version: number;
  limits: Record<string, number>;
  checkpoint: {
    calls?: number;
    steps?: number;
    usage?: Record<string, number>;
    elapsed_ms?: number;
    last_tool?: string;
    unconfirmed_model_request?: boolean;
  };
  proposal: Proposal | null;
  result: (CheckResultData | { error: string }) | null;
  stop_reason: string | null;
  created_at: string;
  updated_at: string;
  provider_label?: string;
  configured_model?: string | null;
  observed_models?: string[];
  private?: boolean;
}

export interface RunEvent {
  id: number;
  run_id?: string;
  kind: string;
  payload: any;
  call_id: string | null;
  created_at: string;
}

export type TokenStatus = "none" | "unverified" | "accepted" | "rejected";
