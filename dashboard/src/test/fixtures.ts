import type { Endpoint, Role, TokenPair } from "../api/types";
import { route } from "./contract";

export const tokens = (role: Role = "admin"): TokenPair => ({ access_token: "test-access", refresh_token: "test-refresh", role });

export const lamp: Endpoint = {
  endpoint_id: "ep_00000001", name: "Ceiling light", room: "bedroom", type: "light", online: true, enabled: true, risk: "normal",
  capabilities: [
    { id: "power", kind: "actuator", type: "bool" },
    { id: "brightness", kind: "actuator", type: "int", min: 0, max: 100, unit: "%" },
  ],
  state: { power: false, brightness: 30 },
};
export const blind: Endpoint = {
  endpoint_id: "ep_00000002", name: "Blind", room: "bedroom", type: "cover", online: true, enabled: true, risk: "normal",
  capabilities: [{ id: "position", kind: "actuator", type: "int", min: 0, max: 100, unit: "%", settle_s: 30 }],
  state: { position: 0 },
};
export const door: Endpoint = {
  endpoint_id: "ep_00000003", name: "Front door", room: "hall", type: "lock", online: true, enabled: true, risk: "confirm",
  capabilities: [{ id: "lock", kind: "actuator", type: "enum", values: ["locked", "unlocked"] }],
  state: { lock: "locked" },
};
export const thermo: Endpoint = {
  endpoint_id: "ep_00000004", name: "Hall sensor", room: "hall", type: "sensor", online: true, enabled: true,
  capabilities: [{ id: "temperature", kind: "sensor", type: "float", unit: "C" }],
  state: { temperature: 21.5 },
};

export const baseRoutes = (role: Role = "admin", eps: Endpoint[] = [lamp, blind, door, thermo]) => [
  route("post", "/auth/login", () => ({ json: tokens(role) })),
  route("post", "/auth/refresh", () => ({ json: tokens(role) })),
  route("get", "/devices", () => ({ json: eps })),
  route("post", "/ws/ticket", () => ({ json: { ticket: "A".repeat(43), expires_in_s: 30 } })),
];
