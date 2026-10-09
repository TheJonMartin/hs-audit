import { createCallbackHandler } from "./lib/handlers.mjs";
import { openTokenStore } from "./lib/store.mjs";

export const config = { path: "/oauth/callback", method: ["GET"] };

export default async (request) =>
  createCallbackHandler({ env: process.env, store: openTokenStore() })(request);
