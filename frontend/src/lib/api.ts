/**
 * Typed client for the Trip Planner API.
 *
 * One place that knows how to talk to the backend, so components deal in
 * domain types rather than in fetch calls and status codes.
 *
 * Every request carries the current Supabase access token. It is read fresh
 * from the client on each call rather than cached, because Supabase refreshes
 * tokens in the background - a cached token would start returning 401s an hour
 * into a session, which is a confusing bug to chase.
 */

import { getSupabase } from "./supabase";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

// ---------------------------------------------------------------------------
// Types - mirror the Pydantic schemas in backend/app/schemas/chat.py
// ---------------------------------------------------------------------------

export type RunStatus = "completed" | "clarifying" | "partial" | "failed";
export type ToolStatus = "ok" | "degraded" | "invalid";

/** Which gear the agent ran in for a turn. */
export type AgentMode = "clarify" | "advise" | "plan";

/** Services the traveller can switch on and off in the composer. */
export type FocusService = "flights" | "stays" | "attractions" | "restaurants";

/**
 * The conversation's slot ledger: what the agent has gathered about the trip
 * so far. Keys mirror backend/app/agent/trip_state.py.
 */
export interface TripState {
  destination?: string;
  origin?: string;
  duration_days?: number;
  travel_window?: string;
  start_date?: string;
  end_date?: string;
  party?: string;
  budget_tier?: string;
  priorities?: string[];
  outline_confirmed?: boolean;
  [key: string]: unknown;
}

export interface PlanStep {
  description: string;
  kind: string;
}

export interface ToolCall {
  tool: string;
  status: ToolStatus;
  source: string;
  latency_ms: number;
}

export interface ChatResponse {
  session_id: string;
  run_id: string;
  response: string;
  status: RunStatus;
  mode: AgentMode;
  trip_state: TripState;
  /** Real district/destination names from the advisor's own retrieval -
   *  populated only in "advise" mode. Render as clickable chips. */
  suggested_options: string[];
  needs_clarification: boolean;
  detected_language: string;
  destination: string | null;
  plan: PlanStep[];
  tool_calls: ToolCall[];
  steps_executed: number;
  replan_count: number;
  latency_ms: number;
}

export interface SessionSummary {
  id: string;
  title: string | null;
  destination: string | null;
  language: string | null;
  message_count: number;
  updated_at: string | null;
}

export interface StoredMessage {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  language: string | null;
  created_at: string | null;
}

export interface Memory {
  id: string;
  memory_type: "preference" | "constraint" | "identity" | "experience";
  subject: string;
  content: string;
  confidence: number;
  mention_count: number;
  status: string;
  source_lang: string | null;
  first_seen_at: string | null;
  last_seen_at: string | null;
}

export interface AdminUser {
  id: string;
  email: string | null;
  display_name: string | null;
  app_role: string;
  last_seen_at: string | null;
  session_count: number;
  message_count: number;
  memory_count: number;
}

export interface RunTrace {
  id: string;
  status: string;
  initial_plan: PlanStep[] | null;
  replan_count: number;
  rag_hops: number;
  detected_language: string | null;
  latency_ms: number | null;
  started_at: string | null;
  steps: {
    step_index: number;
    description: string;
    status: string;
    replan_cycle: number;
    result_summary: string | null;
    error_message: string | null;
    latency_ms: number | null;
  }[];
  tool_calls: {
    tool_name: string;
    arguments: Record<string, unknown>;
    result_summary: string | null;
    succeeded: boolean;
    degraded: boolean;
    error_message: string | null;
    latency_ms: number | null;
  }[];
}

/** An API failure carrying the backend's structured error envelope. */
export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code: string,
    readonly retryable: boolean,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

// ---------------------------------------------------------------------------
// Transport
// ---------------------------------------------------------------------------

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const {
    data: { session },
  } = await getSupabase().auth.getSession();

  if (!session?.access_token) {
    throw new ApiError("You are not signed in.", 401, "unauthenticated", false);
  }

  const response = await fetch(`${API_URL}${path}`, {
    ...init,
    headers: {
      ...init.headers,
      Authorization: `Bearer ${session.access_token}`,
      "Content-Type": "application/json",
    },
  });

  if (response.status === 204) return undefined as T;

  const body = await response.json().catch(() => null);

  if (!response.ok) {
    // The backend returns one envelope for every failure, so this branch
    // handles all of them without per-endpoint error parsing.
    const error = body?.error;
    throw new ApiError(
      error?.message ?? `Request failed with status ${response.status}.`,
      response.status,
      error?.code ?? "unknown_error",
      error?.retryable ?? false,
    );
  }

  return body as T;
}

// ---------------------------------------------------------------------------
// Endpoints
// ---------------------------------------------------------------------------

export const api = {
  chat: (message: string, sessionId?: string, focus?: FocusService[]) =>
    request<ChatResponse>("/api/v1/chat", {
      method: "POST",
      body: JSON.stringify({
        message,
        session_id: sessionId ?? null,
        // A switched-off service is genuinely withheld from the agent - its
        // tool is removed and the planner is told not to plan work for it.
        focus: focus ?? null,
      }),
    }),

  listSessions: () => request<SessionSummary[]>("/api/v1/sessions"),

  getSession: (id: string) =>
    request<{ session: SessionSummary; messages: StoredMessage[] }>(
      `/api/v1/sessions/${id}`,
    ),

  deleteSession: (id: string) =>
    request<void>(`/api/v1/sessions/${id}`, { method: "DELETE" }),

  listMemories: (includeInactive = false) =>
    request<Memory[]>(`/api/v1/me/memories?include_inactive=${includeInactive}`),

  deleteMemory: (id: string) =>
    request<void>(`/api/v1/me/memories/${id}`, { method: "DELETE" }),

  setMemoryEnabled: (enabled: boolean) =>
    request<{ memory_enabled: boolean }>("/api/v1/me/memory-settings", {
      method: "POST",
      body: JSON.stringify({ memory_enabled: enabled }),
    }),

  eraseMyData: () => request<Record<string, number>>("/api/v1/me/data", { method: "DELETE" }),

  admin: {
    listUsers: () => request<AdminUser[]>("/api/v1/admin/users"),
    getUser: (id: string) =>
      request<{
        profile: AdminUser;
        sessions: SessionSummary[];
        memories: Memory[];
      }>(`/api/v1/admin/users/${id}`),
    getSessionMessages: (id: string) =>
      request<StoredMessage[]>(`/api/v1/admin/sessions/${id}/messages`),
    recentRuns: () => request<Record<string, unknown>[]>("/api/v1/admin/runs"),
    getRun: (id: string) => request<RunTrace>(`/api/v1/admin/runs/${id}`),
    toolAnalytics: () => request<Record<string, unknown>[]>("/api/v1/admin/analytics/tools"),
    runAnalytics: () => request<Record<string, unknown>>("/api/v1/admin/analytics/runs"),
    memoryAnalytics: () =>
      request<{ totals: Record<string, number>; breakdown: Record<string, unknown>[] }>(
        "/api/v1/admin/analytics/memory",
      ),
    auditLog: () => request<Record<string, unknown>[]>("/api/v1/admin/audit-log"),
  },
};
