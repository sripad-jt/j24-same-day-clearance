import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api, poll } from "../api";
import type { DeadStockCandidate, DeadStockRunSummary, Store } from "../types";

const fmt = (n: number) => `₹${(+n).toFixed(n % 1 ? 2 : 0)}`;
const DEFAULT_STORE = "BZID-1304298141";

export default function DeadStock() {
  const [stores, setStores] = useState<Store[]>([]);
  const [storeId, setStoreId] = useState(DEFAULT_STORE);
  const [candidates, setCandidates] = useState<DeadStockCandidate[]>([]);
  const [runs, setRuns] = useState<DeadStockRunSummary[]>([]);
  const [speed, setSpeed] = useState(1800);
  const [shadow, setShadow] = useState(false);
  const [source, setSource] = useState<"live" | "sim" | "mock">("live");
  const [autoStart, setAutoStart] = useState(false);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");

  useEffect(() => {
    api.listStores().then(setStores).catch(() => {});
  }, []);
  useEffect(
    () =>
      poll(
        () => api.listDeadStock(storeId),
        3000,
        (d) => {
          setCandidates(d.candidates);
          setRuns(d.runs);
        }
      ),
    [storeId]
  );

  const runByJpin = useMemo(() => {
    const m: Record<string, DeadStockRunSummary> = {};
    for (const r of runs) m[r.jpin] = r;
    return m;
  }, [runs]);

  const discover = async () => {
    setBusy(true);
    setMsg("");
    try {
      await api.deadStockDiscover({
        store_id: storeId,
        auto_start: autoStart,
        demo_speed: speed,
        shadow_mode: shadow,
        mock: source === "mock",
      });
      setMsg(
        autoStart
          ? "Discovery started — flagged items will auto-start clearance."
          : "Discovery started — flagged items will appear below to start manually."
      );
    } finally {
      setBusy(false);
    }
  };

  const startOne = async (jpin: string) => {
    await api.deadStockSeed({
      store_id: storeId,
      jpins: [jpin],
      demo_speed: speed,
      shadow_mode: shadow,
      simulate: source === "sim",
      mock: source === "mock",
    });
    setMsg(`Started clearance for ${jpin}.`);
  };

  return (
    <div>
      <div className="card">
        <div className="row spread">
          <h3>Dead stock — multi-day clearance</h3>
          <span className="muted">{candidates.length} flagged · {runs.length} runs</span>
        </div>
        <div className="row" style={{ alignItems: "center", flexWrap: "wrap", gap: 10 }}>
          <label>
            Store{" "}
            <select value={storeId} onChange={(e) => setStoreId(e.target.value)}>
              <option value={storeId}>{storeId}</option>
              {stores.map((s) => (
                <option key={s.store_id} value={s.store_id}>
                  {s.name} ({s.store_id})
                </option>
              ))}
            </select>
          </label>
          <label>
            Clock speed (×){" "}
            <input type="number" min={1} value={speed} style={{ width: 80 }}
              onChange={(e) => setSpeed(+e.target.value)} />
          </label>
          <label className="checkbox">
            <input type="checkbox" checked={shadow} onChange={(e) => setShadow(e.target.checked)} />
            Shadow
          </label>
          <label>
            Data source{" "}
            <select value={source} onChange={(e) => setSource(e.target.value as any)}>
              <option value="live">Live (Bolt)</option>
              <option value="sim">Live price + simulated sales</option>
              <option value="mock">Mock gateway</option>
            </select>
          </label>
          <label className="checkbox">
            <input type="checkbox" checked={autoStart} onChange={(e) => setAutoStart(e.target.checked)} />
            Auto-start clearance on discover
          </label>
          <button onClick={discover} disabled={busy}>
            {busy ? "Discovering…" : "Discover dead stock"}
          </button>
          {msg && <span className="muted">{msg}</span>}
        </div>
      </div>

      <div className="card">
        <h3>Flagged items</h3>
        {candidates.length === 0 && <p className="muted">No dead stock flagged yet — run discovery.</p>}
        {candidates.length > 0 && (
          <table>
            <thead>
              <tr>
                <th>Product</th>
                <th>Unsold (d)</th>
                <th>Shelf life (d)</th>
                <th>On hand</th>
                <th>Status</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {candidates.map((c) => {
                const run = runByJpin[c.jpin];
                return (
                  <tr key={c.jpin}>
                    <td>{c.product_title || c.jpin}<div className="muted">{c.jpin}</div></td>
                    <td>{c.days_unsold}</td>
                    <td>{c.shelf_life_days || "—"}</td>
                    <td>{c.on_hand || "—"}</td>
                    <td><span className={`chip s-${c.status}`}>{c.status}</span></td>
                    <td>
                      {run ? (
                        <Link to={`/deadstock/runs/${encodeURIComponent(run.run_id)}`}>view run</Link>
                      ) : (
                        <button onClick={() => startOne(c.jpin)}>Start clearance</button>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>

      <div className="card">
        <h3>Clearance runs</h3>
        {runs.length === 0 && <p className="muted">No clearance runs yet.</p>}
        {runs.length > 0 && (
          <table>
            <thead>
              <tr>
                <th>Product</th>
                <th>Status</th>
                <th>On hand</th>
                <th>Price</th>
                <th>Discount</th>
                <th>Days to expiry</th>
                <th>Mode</th>
              </tr>
            </thead>
            <tbody>
              {runs.map((r) => (
                <tr key={r.run_id}>
                  <td>
                    <Link to={`/deadstock/runs/${encodeURIComponent(r.run_id)}`}>
                      {r.product_title || r.jpin}
                    </Link>
                    {r.awaiting_approval && <span className="badge">approval</span>}
                  </td>
                  <td><span className={`chip s-${r.status}`}>{r.status}</span></td>
                  <td>{r.on_hand}</td>
                  <td>{fmt(r.current_price)}<div className="muted">list {fmt(r.list_price)}</div></td>
                  <td>{r.current_discount_pct.toFixed(0)}%</td>
                  <td>{r.days_to_expiry}</td>
                  <td>{r.mode}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
