export async function api(path, options = {}) {
  const response = await fetch(path, {
    signal: options.signal,
    headers: options.body ? { "Content-Type": "application/json" } : undefined,
    method: options.body ? "POST" : "GET",
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  let payload;
  try {
    payload = await response.json();
  } catch {
    throw new Error(`Local server returned HTTP ${response.status}.`);
  }
  if (!response.ok) throw new Error(payload.error || `Local server returned HTTP ${response.status}.`);
  return payload;
}
