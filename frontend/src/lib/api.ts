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

/** What sort of thing an itinerary item is. Mirrors ItemKind on the backend. */
export type ItemKind =
  | "sight"
  | "food"
  | "activity"
  | "transport"
  | "stay"
  | "neighbourhood"
  | "note";

export interface ItineraryItem {
  name: string;
  kind: ItemKind;
  district: string | null;
  description: string;
  latitude: number | null;
  longitude: number | null;
  approx_duration: string | null;
  booking_note: string | null;
}

export interface ItineraryDay {
  day_number: number;
  title: string;
  date: string | null;
  summary: string;
  morning: ItineraryItem[];
  afternoon: ItineraryItem[];
  evening: ItineraryItem[];
  all_day: ItineraryItem[];
}

/** A full plan as structured days. Absent for scoped asks and advisory turns. */
export interface Itinerary {
  destination: string;
  intro: string;
  days: ItineraryDay[];
  practical_notes: string[];
  gaps: string[];
}

/** A follow-up offer: the chip label, and the message tapping it sends. */
export interface SuggestedAction {
  label: string;
  message: string;
}

/**
 * A photograph for a place.
 *
 * `is_representative` is the one that matters: when true this is not a photo
 * of that specific place, only of something similar, and the interface has to
 * say so rather than implying otherwise.
 */
export interface PlaceImageResult {
  url: string;
  tier: "wikivoyage" | "wikipedia" | "commons_geo" | "loremflickr";
  is_representative: boolean;
  credit: string | null;
  source_page: string | null;
}

export interface ToolCall {
  tool: string;
  status: ToolStatus;
  source: string;
  latency_ms: number;
}

/**
 * One structured finding, with where it came from.
 *
 * `simulated` is the field that matters and is never optional: the flight and
 * hotel providers are mocks, and a generated price rendered like a real one is
 * the most misleading thing this interface could show.
 */
export interface TripFact {
  panel: string;
  tool: string;
  source: string;
  simulated: boolean;
  data: Record<string, unknown>;
}

/** Structured findings keyed by the panel that renders them. */
export interface TripFacts {
  weather?: TripFact;
  stays?: TripFact;
  flights?: TripFact;
  places?: TripFact;
}

/** One line of "here is what I am doing", streamed while a turn runs. */
export interface ProgressEvent {
  kind: "stage" | "step" | "tool" | "note";
  message: string;
  detail?: string;
  data?: Record<string, unknown>;
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
  /** The same options with coordinates, so an advisory turn can be mapped and
   *  illustrated rather than being a list of bare names. */
  suggested_places: { name: string; latitude: number; longitude: number }[];
  /** Structured tool findings for the trip panel, with provenance. */
  trip_facts: TripFacts;
  /** The plan as structured days, when this turn produced one. */
  itinerary: Itinerary | null;
  /** Follow-up offers derived server-side from what has not run yet. */
  suggested_actions: SuggestedAction[];
  needs_clarification: boolean;
  detected_language: string;
  destination: string | null;
  /** Which model providers actually served this turn - "groq", "local", or
   *  both when the cloud quota ran out part-way through. Shown in the UI so a
   *  slow reply reads as "running on your machine" rather than "hung". */
  llm_providers: string[];
  plan: PlanStep[];
  tool_calls: ToolCall[];
  steps_executed: number;
  replan_count: number;
  latency_ms: number;
  /** Tokens this turn consumed. Exposed because on a free tier the
   *  per-minute token allowance - not the request count - is what limits
   *  throughput, and you cannot budget for what you cannot see. */
  total_tokens: number;
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
  /** What an assistant reply needs to be drawn again - itinerary, advisory
   *  options and their coordinates. Empty for user messages and for replies
   *  written before this was persisted. */
  metadata?: Record<string, unknown>;
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

/**
 * One key's position against its daily allowance.
 *
 * `measured` is the field that matters. Groq publishes no daily quota, so
 * every figure here is this server's own arithmetic until Groq refuses a call
 * and quotes real numbers - at which point that model flips to measured.
 * Rendering an estimate and a measurement identically would be the same class
 * of dishonesty as the simulated-price badge elsewhere in this interface.
 */
export interface KeyQuotaModel {
  model: string;
  used: number;
  limit: number;
  remaining: number;
  measured: boolean;
}

export interface KeyQuota {
  key: string;
  available: boolean;
  cooldown_remaining_s: number;
  uses: number;
  rate_limits: number;
  errors: number;
  quota: {
    used: number;
    limit: number;
    remaining: number;
    models: KeyQuotaModel[];
  };
}

/** What this deployment can actually do, and what budget is left. */
export interface SystemStatus {
  environment: string;
  llm_provider_mode: "groq" | "local" | "auto";
  local_llm: {
    available: boolean;
    reason: string;
    models: string[];
  };
  quota:
    | { configured: false; keys: []; total: null }
    | {
        configured: true;
        provider: string;
        total_keys: number;
        available_keys: number;
        keys: KeyQuota[];
        total: { used: number; limit: number; remaining: number };
        basis: string;
      };
  /** True when this deployment has opened the trace dashboard to everyone.
   *  Read from the server rather than assumed, so the nav link and the
   *  endpoint's real policy cannot disagree and produce an item that 403s. */
  public_admin_dashboard: boolean;
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

/** Bearer token plus JSON content type, or throw if signed out. */
async function authHeaders(): Promise<Record<string, string>> {
  const {
    data: { session },
  } = await getSupabase().auth.getSession();

  if (!session?.access_token) {
    throw new ApiError("You are not signed in.", 401, "unauthenticated", false);
  }

  return {
    Authorization: `Bearer ${session.access_token}`,
    "Content-Type": "application/json",
  };
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${API_URL}${path}`, {
    ...init,
    headers: { ...init.headers, ...(await authHeaders()) },
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
  chat: (
    message: string,
    sessionId?: string,
    focus?: FocusService[],
    localOnly?: boolean,
  ) =>
    request<ChatResponse>("/api/v1/chat", {
      method: "POST",
      body: JSON.stringify({
        message,
        session_id: sessionId ?? null,
        // A switched-off service is genuinely withheld from the agent - its
        // tool is removed and the planner is told not to plan work for it.
        focus: focus ?? null,
        // Off by default: the cloud is faster, and the server already falls
        // back to the local model on its own once quota runs out.
        local_only: localOnly ?? false,
      }),
    }),

  /**
   * Same turn as `chat`, but reports what the agent is doing while it does it.
   *
   * `fetch` with a reader rather than `EventSource`, because EventSource only
   * issues GETs and cannot carry an Authorization header - this endpoint needs
   * both a JSON body and a bearer token.
   *
   * Falls back to nothing clever on failure: the caller catches and can retry
   * against the plain endpoint, so a proxy that mangles streaming degrades to
   * the old silent-but-working behaviour rather than breaking chat entirely.
   */
  chatStream: async (
    message: string,
    options: {
      sessionId?: string;
      focus?: FocusService[];
      localOnly?: boolean;
      onProgress: (event: ProgressEvent) => void;
      signal?: AbortSignal;
    },
  ): Promise<ChatResponse> => {
    const response = await fetch(`${API_URL}/api/v1/chat/stream`, {
      method: "POST",
      headers: await authHeaders(),
      signal: options.signal,
      body: JSON.stringify({
        message,
        session_id: options.sessionId ?? null,
        focus: options.focus ?? null,
        local_only: options.localOnly ?? false,
      }),
    });

    if (!response.ok || !response.body) {
      throw new ApiError(
        `The agent could not be reached (${response.status}).`,
        response.status,
        "stream_failed",
        true,
      );
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let result: ChatResponse | null = null;

    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      // SSE frames are separated by a blank line. A chunk can split one in
      // half, so the trailing partial frame stays in the buffer.
      const frames = buffer.split("\n\n");
      buffer = frames.pop() ?? "";

      for (const frame of frames) {
        const line = frame.split("\n").find((l) => l.startsWith("data: "));
        if (!line) continue;
        const event = JSON.parse(line.slice(6));

        if (event.kind === "done") {
          if (event.error) throw new ApiError(event.error, 502, "agent_failed", true);
          result = event.result as ChatResponse;
        } else {
          options.onProgress(event as ProgressEvent);
        }
      }
    }

    if (!result) {
      throw new ApiError(
        "The agent stopped without replying.",
        502,
        "empty_stream",
        true,
      );
    }
    return result;
  },

  /**
   * What this deployment can do and how much cloud budget is left.
   *
   * Asked of the server rather than inferred in the browser: whether the local
   * model is usable depends on what the *API* can reach, not on where the page
   * is being viewed from. Running the frontend locally against the hosted API
   * would fool any client-side guess.
   */
  systemStatus: () => request<SystemStatus>("/api/v1/system/status"),

  listSessions: () => request<SessionSummary[]>("/api/v1/sessions"),

  /** Resolve a photograph for a place. Called lazily per card so a reply is
   *  never held up waiting for pictures. */
  placeImage: (params: {
    name: string;
    destination?: string | null;
    latitude?: number | null;
    longitude?: number | null;
    kind?: string | null;
  }) => {
    const query = new URLSearchParams({ name: params.name });
    if (params.destination) query.set("destination", params.destination);
    if (params.latitude != null) query.set("latitude", String(params.latitude));
    if (params.longitude != null) query.set("longitude", String(params.longitude));
    if (params.kind) query.set("kind", params.kind);
    return request<PlaceImageResult>(`/api/v1/images/place?${query.toString()}`);
  },

  getSession: (id: string) =>
    request<{ session: SessionSummary; messages: StoredMessage[] }>(
      `/api/v1/sessions/${id}`,
    ),

  deleteSession: (id: string) =>
    request<void>(`/api/v1/sessions/${id}`, { method: "DELETE" }),

  listMemories: (includeInactive = false) =>
    request<Memory[]>(`/api/v1/me/memories?include_inactive=${includeInactive}`),

  addMemory: (body: {
    content: string;
    memory_type?: "constraint" | "preference" | "identity" | "experience";
    subject?: string;
  }) =>
    request<Memory>("/api/v1/me/memories", {
      method: "POST",
      body: JSON.stringify(body),
    }),

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
