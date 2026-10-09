import { createTokenHandler } from "./lib/handlers.mjs";
import { openTokenStore } from "./lib/store.mjs";

export const config = { path: ["/api/token", "/api/clients"], method: ["GET", "PUT", "DELETE"] };

export default async (request) =>
  createTokenHandler({ env: process.env, store: openTokenStore() })(request);
