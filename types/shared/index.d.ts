export { BuySell, Fill, Trade, WebhookPayloadTrades } from "./types";

// Discriminated union — grows as new event types are added.
export type WebhookPayload = WebhookPayloadTrades;
