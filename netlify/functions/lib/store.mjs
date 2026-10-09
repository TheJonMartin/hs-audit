import { getStore } from "@netlify/blobs";

/**
 * Open the token store.
 *
 * A site-scoped Blobs store is shared by every deploy context (production, previews, branch
 * deploys), so the production store name comes from TOKEN_STORE_NAME, which must be set in the
 * Production context only. Every other context falls back to a separate empty store.
 * Reads must see the latest write (install, then fetch seconds later), hence strong consistency.
 */
export function openTokenStore(env = process.env) {
  return getStore({ name: env.TOKEN_STORE_NAME || "oauth-tokens-nonproduction", consistency: "strong" });
}
