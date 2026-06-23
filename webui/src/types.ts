export type Autonomy = "manual" | "assist" | "auto";

export type Mode = "explore" | "command" | "conversational";

export type RobotVariant = "GENERIC" | "SE" | "AIR" | "AIR2" | "PRO";

export interface Settings {
  robot_link: "native" | "mock" | "native_x86" | "air2" | "air2_native";
  robot_variant: RobotVariant;
  ai_provider: string;
  ai_base_url: string;
  ai_api_key: string;
  ai_api_key_set: boolean;
  ai_model: string;
  ai_summarizer_model: string;
  ai_vision_model: string;
  setup_complete: boolean;
  talk_enabled: boolean;
  confirm_motion: boolean;
  allow_think: boolean;
  allow_motion: boolean;
  allow_video: boolean;
  allow_audio_in: boolean;
  asleep: boolean;
  overseer: boolean;
  supervisor: boolean;
  ai_supervisor_model: string;
  autonomy: Autonomy;
  mode: Mode;
  directive: string;
  max_speed: number;
  tick_seconds: number;
  goal: string;
  tts_engine: "piper" | "os";
  voice: string;
  autodock_pct: number;
  robot_name: string;
  persona: string;
  owner_name: string;
  require_name: boolean;
  obey_owner_only: boolean;
}

export interface TtsState {
  available: boolean;
  backend: string;
  voices?: string[];
  engine?: string;
  voice?: string;
}

export interface PendingApproval {
  id: string;
  tool: string;
  args: Record<string, unknown>;
  requester: string;
  reason: string;
  ts: number;
}

export interface Identity {
  owner: string;
  present: string[];
  recognizer: boolean;
  authority_active: boolean;
  pending: PendingApproval[];
}

export interface Provider {
  key: string;
  name: string;
  base_url: string;
  needs_key: boolean;
  fast: string;
  heavy: string;
  notes: string;
}

export interface Telemetry {
  connected?: boolean;
  paused?: boolean;
  awake?: boolean;
  battery?: number;
  charge?: number;
  codec?: string | null;
  frames_received?: number;
  toggles?: Record<string, boolean | null>;
  eyes_animation?: string | null;
  eye_animations?: string[];
  audio_out?: { sent: number; available: boolean | null } | null;
  resting?: boolean;
  sleeping?: boolean;
  video_frames?: number;
  touched?: boolean;
  imu?: Record<string, number> | number[];
  gyro?: Record<string, number> | number[];
  tof?: number;
  distance?: number;
  obstacle?: boolean;
  wifi?: number;
  wifiStrength?: number;
  laser?: number;
  moveSpeed?: number;
  moveMode?: number;
  lowBatteryPercentage?: number;
  avoidobstacle?: boolean;
}

export interface SlamMap {
  enabled: boolean;
  pose: { x: number; y: number; yaw_deg: number };
  trail: [number, number][];
  keyframes: number;
  frames: number;
}

export interface BrainStatus {
  status: string;
  error: string | null;
  last_tick_ts: number;
  autonomy: Autonomy;
  running: boolean;
  behavior?: { scope: string; intent: string; detail?: string; voice_intent?: string | null; idle_s?: number };
  calibrated?: boolean;
}

export type AutobotEvent =
  | { type: "hello"; settings: Settings; brain: BrainStatus; tts: TtsState; identity?: Identity }
  | { type: "settings"; changed: string[]; settings: Settings }
  | { type: "telemetry"; telemetry: Telemetry }
  | { type: "thought"; text: string; ts: number }
  | { type: "tool_call"; name: string; args: Record<string, unknown>; ts: number }
  | { type: "tool_result"; name: string; result: Record<string, unknown>; ts: number }
  | { type: "observation"; summary: string; telemetry: Telemetry; ts: number }
  | { type: "status"; status: string; error: string | null; ts: number }
  | { type: "speech"; text: string; b64: string; sr: number; ts: number }
  | { type: "error"; error: string; ts: number }
  | { type: "estop"; ok: boolean }
  | { type: "approval_request"; id: string; tool: string; args: Record<string, unknown>; requester: string; reason: string; ts: number }
  | { type: "approval_resolved"; id: string; approved: boolean; ts: number }
  | { type: "proposal"; seq: number; verb: string; args: Record<string, unknown>; ts: number }
  | { type: "overseer_act"; kind: string; args: Record<string, unknown>; result: Record<string, unknown>; ts: number };

export interface OverseerLogItem {
  id: number;
  // "proposal" = what the paralyzed brain tried to do; "act" = what the overseer actually sent to the robot.
  kind: "proposal" | "act";
  verb: string;
  args: Record<string, unknown>;
  result?: Record<string, unknown>;
  ts: number;
}

export interface FeedItem {
  id: number;
  kind: "thought" | "action" | "result" | "sees" | "error" | "estop" | "approval" | "heard";
  text: string;
  detail?: string;
  ts: number;
}
