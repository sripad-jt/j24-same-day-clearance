import type {
  Candidate,
  DeadStockCandidate,
  DeadStockRunDetail,
  DeadStockRunSummary,
  InventorySnapshot,
  OfferOutcome,
  RunDetail,
  RunSummary,
  Store,
} from "./types";

export const API = `${import.meta.env.BASE_URL}api`.replace("//api", "/api");

async function j<T>(r: Response): Promise<T> {
  if (!r.ok) throw new Error((await r.text()) || r.statusText);
  return r.json();
}

function post<T>(path: string, body?: unknown): Promise<T> {
  return fetch(`${API}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  }).then((r) => j<T>(r));
}

export const api = {
  listRuns: () => fetch(`${API}/runs`).then((r) => j<RunSummary[]>(r)),
  getRun: (id: string) =>
    fetch(`${API}/runs/${encodeURIComponent(id)}`).then((r) => j<RunDetail>(r)),
  getConfig: () => fetch(`${API}/config`).then((r) => j<any>(r)),
  listStores: () => fetch(`${API}/stores`).then((r) => j<Store[]>(r)),
  listCandidates: (storeId: string) =>
    fetch(`${API}/candidates?store_id=${encodeURIComponent(storeId)}`).then((r) =>
      j<{ store: Store | null; candidates: Candidate[] }>(r)
    ),
  getInventory: (storeId: string, refresh = false) =>
    fetch(`${API}/inventory?store_id=${encodeURIComponent(storeId)}${refresh ? "&refresh=true" : ""}`).then((r) =>
      j<InventorySnapshot>(r)
    ),
  seed: (body: {
    store_id: string;
    shadow_mode: boolean;
    demo_speed: number;
    jpins?: string[];
    count?: number;
    include_rte?: boolean;
    simulate?: boolean;
    mock?: boolean;
  }) => post<{ started: string[] }>("/runs/seed", body),
  decide: (id: string, approve: boolean, rung: string) =>
    post(`/runs/${encodeURIComponent(id)}/decision`, { rung, approve }),
  override: (id: string, action: string, rung?: string) =>
    post(`/runs/${encodeURIComponent(id)}/override`, { action, rung }),
  grn: (id: string, qty: number) =>
    post(`/runs/${encodeURIComponent(id)}/grn`, { qty }),
  soldOut: (id: string) => post(`/runs/${encodeURIComponent(id)}/soldout`),
  simulate: (
    id: string,
    body: { units_sold?: number; recent_rate?: number; q0?: number }
  ) => post(`/runs/${encodeURIComponent(id)}/simulate`, body),
  setStandingRule: (id: string, pct: number) =>
    post(`/runs/${encodeURIComponent(id)}/standing-rule`, {
      auto_approve_max_discount_pct: pct,
    }),
  listStoreOffers: (storeId: string) =>
    fetch(`${API}/stores/${encodeURIComponent(storeId)}/offers`).then((r) =>
      j<OfferOutcome[]>(r)
    ),

  // --- dead stock (multi-day clearance) ---
  listDeadStock: (storeId: string) =>
    fetch(`${API}/deadstock?store_id=${encodeURIComponent(storeId)}`).then((r) =>
      j<{ candidates: DeadStockCandidate[]; runs: DeadStockRunSummary[] }>(r)
    ),
  getDeadStockRun: (id: string) =>
    fetch(`${API}/deadstock/runs/${encodeURIComponent(id)}`).then((r) =>
      j<DeadStockRunDetail>(r)
    ),
  deadStockDiscover: (body: {
    store_id: string;
    auto_start: boolean;
    demo_speed: number;
    shadow_mode: boolean;
    mock?: boolean;
  }) => post<{ started: string }>("/deadstock/discover", body),
  deadStockSeed: (body: {
    store_id: string;
    jpins: string[];
    demo_speed: number;
    shadow_mode: boolean;
    simulate: boolean;
    mock?: boolean;
  }) => post<{ started: string[] }>("/deadstock/seed", body),
};

export function poll<T>(fn: () => Promise<T>, ms: number, cb: (v: T) => void) {
  let alive = true;
  const tick = async () => {
    try {
      const v = await fn();
      if (alive) cb(v);
    } catch {
      /* ignore transient errors */
    }
    if (alive) setTimeout(tick, ms);
  };
  tick();
  return () => {
    alive = false;
  };
}
