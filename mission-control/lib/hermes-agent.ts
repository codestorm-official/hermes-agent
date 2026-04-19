// Thin helper for calling the hermes-agent admin API with basic auth.
// The admin API is behind HTTP basic auth (Ari:Savta911!); we pull the
// three values from env vars set on the mission-control Railway service:
//   HERMES_AGENT_URL   - e.g. https://hermes-agent-production-ad27.up.railway.app
//   HERMES_ADMIN_USER  - e.g. Ari
//   HERMES_ADMIN_PASS  - e.g. Savta911!
// These routes run server-side (Next.js app/api/...) so the password never
// reaches the browser.

function cfg() {
  const url = process.env.HERMES_AGENT_URL;
  const user = process.env.HERMES_ADMIN_USER;
  const pass = process.env.HERMES_ADMIN_PASS;
  if (!url || !user || !pass) {
    throw new Error(
      'HERMES_AGENT_URL, HERMES_ADMIN_USER, HERMES_ADMIN_PASS must be set',
    );
  }
  return { url: url.replace(/\/$/, ''), auth: 'Basic ' + Buffer.from(`${user}:${pass}`).toString('base64') };
}

export async function hermesGet<T = unknown>(path: string): Promise<T> {
  const { url, auth } = cfg();
  const r = await fetch(`${url}${path}`, {
    headers: { Authorization: auth },
    cache: 'no-store',
  });
  if (!r.ok) throw new Error(`GET ${path} -> ${r.status}`);
  return (await r.json()) as T;
}

export async function hermesPost<T = unknown>(
  path: string,
  body: unknown,
): Promise<T> {
  const { url, auth } = cfg();
  const r = await fetch(`${url}${path}`, {
    method: 'POST',
    headers: { Authorization: auth, 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    cache: 'no-store',
  });
  const text = await r.text();
  let data: any;
  try { data = JSON.parse(text); } catch { data = { raw: text }; }
  if (!r.ok) throw new Error(`POST ${path} -> ${r.status} · ${text.slice(0, 200)}`);
  return data as T;
}
