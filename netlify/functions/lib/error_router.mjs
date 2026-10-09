// Catch, Log, Fail: post the Unified Error Payload to the Central Error Router.
const TIMEOUT_MS = 5000;
const MAX_ATTEMPTS = 2;
const BACKOFF_MS = 500;
const HTTP_SERVER_ERROR_MIN = 500;

/**
 * Post a payload with one bounded retry on network error, timeout or 5xx (never 4xx).
 * Never throws; falls back to stderr.
 */
export async function postToErrorRouter(env, payload, fetchImpl = fetch) {
  for (let attempt = 1; attempt <= MAX_ATTEMPTS; attempt++) {
    let status = null;
    let detail = "network";
    try {
      const response = await fetchImpl(env.ERROR_ROUTER_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
        signal: AbortSignal.timeout(TIMEOUT_MS),
      });
      if (response.status < HTTP_SERVER_ERROR_MIN) return;
      status = response.status;
      detail = `HTTP ${status}`;
    } catch (err) {
      detail = err.name;
    }
    if (attempt === MAX_ATTEMPTS) {
      console.error(`CRITICAL: Central Error Router post failed (attempt ${attempt}/${MAX_ATTEMPTS}, status=${status ?? "network"}): ${detail}. Payload:`, JSON.stringify(payload));
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, BACKOFF_MS));
  }
}

/** Build and post the Unified Error Payload. Never throws. */
export async function handleFailure(env, error, context, fetchImpl = fetch) {
  const payload = {
    record_id: context.record_id || "N/A",
    flow_id: env.FLOW_ID || "unknown",
    flow_name: env.FLOW_NAME || "Unnamed_Flow",
    process_name: context.process_name || "Unknown step",
    error_details: `${error.name}: ${error.message}`,
    source: env.PLATFORM_NAME || "OTHER",
  };
  if (context.correlation_id) payload.correlation_id = context.correlation_id;
  await postToErrorRouter(env, payload, fetchImpl);
}
