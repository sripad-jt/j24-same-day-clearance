import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api, poll } from "../api";
import type { Candidate, InventoryItem, RunSummary, Store } from "../types";

const fmt = (n: number) => `₹${(+n).toFixed(n % 1 ? 2 : 0)}`;
const DEFAULT_STORE = "BZID-1304298141"; // J24 - Essentials BTM Layout

export default function Dashboard() {
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [stores, setStores] = useState<Store[]>([]);
  const [storeId, setStoreId] = useState(DEFAULT_STORE);
  const [storeFilter, setStoreFilter] = useState("");
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [inv, setInv] = useState<Record<string, InventoryItem>>({});
  const [invSource, setInvSource] = useState<string>("");
  const [invLoading, setInvLoading] = useState(false);
  const [speed, setSpeed] = useState(1800);
  const [shadow, setShadow] = useState(false);
  const [sim, setSim] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => poll(api.listRuns, 2000, setRuns), []);
  useEffect(() => {
    api.listStores().then(setStores).catch(() => {});
  }, []);

  // Load (and default-select) the leafy-green candidates whenever the store changes.
  useEffect(() => {
    api
      .listCandidates(storeId)
      .then((r) => {
        setCandidates(r.candidates);
        setSelected(new Set(r.candidates.map((c) => c.jpin)));
      })
      .catch(() => {});
  }, [storeId]);

  // Pull inventory snapshot — returns immediately from cache, polls while loading.
  const fetchInventory = (sid: string, refresh = false) => {
    if (!refresh) { setInv({}); setInvSource(""); }
    setInvLoading(true);
    api
      .getInventory(sid, refresh)
      .then((r) => {
        setInv(Object.fromEntries(r.items.map((i) => [i.jpin, i])));
        setInvSource(r.source);
        if (r.loading) {
          // Background fetch in progress — poll every 5s until it settles.
          setTimeout(() => fetchInventory(sid, false), 5000);
        } else {
          setInvLoading(false);
        }
      })
      .catch(() => { setInvSource("error"); setInvLoading(false); });
  };

  useEffect(() => { fetchInventory(storeId); }, [storeId]);

  const visibleStores = useMemo(() => {
    const q = storeFilter.trim().toLowerCase();
    if (!q) return stores;
    return stores.filter(
      (s) => s.name.toLowerCase().includes(q) || s.store_id.toLowerCase().includes(q)
    );
  }, [stores, storeFilter]);

  const toggle = (jpin: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      next.has(jpin) ? next.delete(jpin) : next.add(jpin);
      return next;
    });
  const allSelected = candidates.length > 0 && selected.size === candidates.length;
  const toggleAll = () =>
    setSelected(allSelected ? new Set() : new Set(candidates.map((c) => c.jpin)));

  const start = async () => {
    setBusy(true);
    try {
      await api.seed({
        store_id: storeId,
        jpins: [...selected],
        shadow_mode: shadow,
        demo_speed: speed,
        simulate: sim,
      });
      setRuns(await api.listRuns());
    } finally {
      setBusy(false);
    }
  };

  return (
    <div>
      <div className="card">
        <h3>Start clearance runs</h3>
        <p className="muted">
          Pick a store, choose the leafy greens (L=1, must clear today), and start
          one durable Temporal workflow per line. High clock-speed replays a full
          selling day in ~30s.
        </p>
        <div className="row">
          <label style={{ flex: "1 1 320px" }}>
            Store
            <input
              type="text"
              placeholder="Filter stores…"
              value={storeFilter}
              onChange={(e) => setStoreFilter(e.target.value)}
            />
            <select value={storeId} onChange={(e) => setStoreId(e.target.value)}>
              {visibleStores.map((s) => (
                <option key={s.store_id} value={s.store_id}>
                  {s.name} ({s.store_id})
                </option>
              ))}
            </select>
          </label>
          <label>
            Clock speed (×)
            <input
              type="number"
              min={1}
              value={speed}
              onChange={(e) => setSpeed(+e.target.value)}
            />
          </label>
          <label className="checkbox">
            <input
              type="checkbox"
              checked={shadow}
              onChange={(e) => setShadow(e.target.checked)}
            />
            Shadow mode (no price writes)
          </label>
          <label className="checkbox">
            <input
              type="checkbox"
              checked={sim}
              onChange={(e) => setSim(e.target.checked)}
            />
            Simulate (live price, edit sell-through in the run)
          </label>
        </div>

        <div className="row spread" style={{ marginTop: 12, alignItems: "center" }}>
          <h4 style={{ margin: 0 }}>Leafy greens</h4>
          <span className="muted" style={{ display: "flex", alignItems: "center", gap: 6 }}>
            sell-through source:{" "}
            <span className={`chip ${invSource === "live" ? "s-OBSERVING" : invSource === "error" ? "d-HOLD" : invSource === "partial" ? "await" : ""}`}>
              {invLoading ? "loading…" : invSource || "—"}
            </span>
            {invSource === "live" && " (live — as of 5 AM IST)"}
            {invSource === "partial" && " (some JPINs timed out — shown where available)"}
            {invSource === "loading" && " (fetching from Bolt, up to 2 min…)"}
            {invSource === "error" && " (API unavailable)"}
            <button
              title="Refresh inventory"
              disabled={invLoading}
              onClick={() => fetchInventory(storeId, true)}
              style={{
                background: "none", border: "none", cursor: invLoading ? "default" : "pointer",
                padding: "0 2px", opacity: invLoading ? 0.4 : 1, lineHeight: 1,
                display: "inline-flex", alignItems: "center", color: "inherit",
              }}
            >
              <svg
                xmlns="http://www.w3.org/2000/svg"
                width="14" height="14" viewBox="0 0 24 24"
                fill="none" stroke="currentColor" strokeWidth="2.5"
                strokeLinecap="round" strokeLinejoin="round"
                style={{ animation: invLoading ? "spin 1s linear infinite" : "none" }}
              >
                <polyline points="23 4 23 10 17 10" />
                <polyline points="1 20 1 14 7 14" />
                <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15" />
              </svg>
            </button>
          </span>
        </div>
        <table style={{ marginTop: 8 }}>
          <thead>
            <tr>
              <th style={{ width: 32 }}>
                <input type="checkbox" checked={allSelected} onChange={toggleAll} />
              </th>
              <th>Leafy green</th>
              <th>JPIN</th>
              <th>At 5 AM</th>
              <th>Received today</th>
              <th>Sold today</th>
              <th>List price</th>
            </tr>
          </thead>
          <tbody>
            {candidates.map((c) => {
              const item = inv[c.jpin];
              const dash = (title?: string) =>
                invLoading ? "…" : <span className="muted" title={title}>—</span>;
              const num = (v: number | null | undefined, title?: string) =>
                v != null ? v.toLocaleString() : dash(title);
              return (
                <tr key={c.jpin}>
                  <td>
                    <input
                      type="checkbox"
                      checked={selected.has(c.jpin)}
                      onChange={() => toggle(c.jpin)}
                    />
                  </td>
                  <td>{c.product_title}</td>
                  <td className="muted">{c.jpin}</td>
                  <td title="Total units on hand at 05:00 IST (on_hand now + sold today)">
                    {num(item?.inventory_at_t0, "Active + OUTWARDED query incomplete")}
                  </td>
                  <td title="GRN lots inwarded since 05:00 IST">
                    {num(item?.received_today, "Active stock query timed out")}
                  </td>
                  <td title="OUTWARDED units since 05:00 IST">
                    {num(item?.sold_today, "OUTWARDED query timed out")}
                  </td>
                  <td title="placeholder — API listingSellingPrice is ₹1">
                    {fmt(c.list_price)}<span className="muted"> *</span>
                  </td>
                </tr>
              );
            })}
            {candidates.length === 0 && (
              <tr>
                <td colSpan={5} className="muted">
                  No leafy-green candidates for this store.
                </td>
              </tr>
            )}
          </tbody>
        </table>

        <div className="row" style={{ marginTop: 12 }}>
          <button onClick={start} disabled={busy || selected.size === 0}>
            {busy
              ? "Starting…"
              : `Start ${selected.size} clearance run${selected.size === 1 ? "" : "s"}`}
          </button>
        </div>
      </div>

      <div className="card">
        <h3>Runs ({runs.length})</h3>
        <table>
          <thead>
            <tr>
              <th>Line</th>
              <th>Status</th>
              <th>Rung</th>
              <th>Price</th>
              <th>Sold / Q0</th>
              <th>Mode</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {runs.map((r) => (
              <tr key={r.run_id} className={r.awaiting_approval ? "await-row" : ""}>
                <td>
                  <Link to={`/runs/${encodeURIComponent(r.run_id)}`}>
                    {r.product_title || r.jpin}
                  </Link>
                  {r.is_rte && <span className="tag rte">RTE</span>}
                </td>
                <td>
                  <span className={`chip s-${r.status}`}>{r.status}</span>
                  {r.awaiting_approval && (
                    <span className="chip await">NEEDS APPROVAL</span>
                  )}
                </td>
                <td>{r.current_rung}</td>
                <td>
                  {fmt(r.current_price)}
                  {r.current_price < r.list_price && (
                    <span className="strike">{fmt(r.list_price)}</span>
                  )}
                </td>
                <td>
                  {r.units_sold} / {r.q0}
                </td>
                <td>{r.shadow_mode ? "shadow" : "live"}</td>
                <td>
                  <Link className="link" to={`/runs/${encodeURIComponent(r.run_id)}`}>
                    view →
                  </Link>
                </td>
              </tr>
            ))}
            {runs.length === 0 && (
              <tr>
                <td colSpan={7} className="muted">
                  No runs yet — seed some above.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
