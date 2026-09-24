// One place that talks to the backend.
//
// Every request carries the acting principal in the X-Proof-Principal header,
// never in the body -- the same "identity from the transport, not the
// conversation" rule the agent layer enforces, applied at the browser edge.
// The selected principal lives in localStorage so a refresh keeps you "logged
// in" as the same person.

const PRINCIPAL_KEY = "proof.principal";

export function getPrincipal() {
  try {
    return localStorage.getItem(PRINCIPAL_KEY) || "a.morin";
  } catch {
    return "a.morin";
  }
}

export function setPrincipal(id) {
  try {
    localStorage.setItem(PRINCIPAL_KEY, id);
  } catch {
    /* private mode: fall back to the default each load */
  }
}

async function request(path, { method = "GET", body } = {}) {
  const res = await fetch(path, {
    method,
    headers: {
      "Content-Type": "application/json",
      "X-Proof-Principal": getPrincipal(),
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  const data = text ? JSON.parse(text) : null;
  if (!res.ok) {
    // FastAPI puts the human-readable reason in `detail`; surface it so the
    // UI can show the authz layer's actual words (e.g. a 403 refusal).
    const message = (data && data.detail) || `${res.status} ${res.statusText}`;
    throw Object.assign(new Error(message), { status: res.status });
  }
  return data;
}

export const api = {
  principals: () => request("/api/principals"),
  whoami: () => request("/api/whoami"),
  dashboard: (plant) =>
    request(`/api/dashboard${plant ? `?plant=${encodeURIComponent(plant)}` : ""}`),
  pending: (plant) =>
    request(`/api/actions/pending${plant ? `?plant=${encodeURIComponent(plant)}` : ""}`),
  action: (id) => request(`/api/actions/${id}`),
  decide: (id, event, note) =>
    request(`/api/actions/${id}/decision`, { method: "POST", body: { event, note } }),
  askStatus: () => request("/api/ask/status"),
  ask: (question) => request("/api/ask", { method: "POST", body: { question } }),
};
