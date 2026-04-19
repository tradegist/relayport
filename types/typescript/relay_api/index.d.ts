import { WebhookPayloadTrades } from "./types";
export type { HealthResponse, RunPollResponse, WebhookPayloadTrades } from "./types";

// Discriminated union — grows as new event types are added.
export type WebhookPayload = WebhookPayloadTrades;
