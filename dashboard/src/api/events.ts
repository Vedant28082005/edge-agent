// Wire types for /ws messages and /chat SSE frames, generated from gateway.yaml (WsMessage, SseEvent). Never hand-write them.
import type { components } from "./schema.gen";

type S = components["schemas"];
export type WsMessage = S["WsMessage"];
export type SseEvent = S["SseEvent"];
export type ConfirmationRequest = S["ConfirmationRequestFields"];
export type EventAction = S["EventAction"];
export type ConfirmationRequired = S["ConfirmationRequired"];
