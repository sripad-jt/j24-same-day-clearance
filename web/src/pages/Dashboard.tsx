import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, poll } from "../api";
import type { RunSummary } from "../types";

const fmt = (n: number) => `₹${(+n).toFixed(n % 1 ? 2 : 0)}`;

export default function Dashboard() {
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [count, setCount] = useState(6);
  const [speed, setSpeed] = useState(1800);
  const [shadow, setShadow] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => poll(api.listRuns, 2000, setRuns), []);

  const seed = async () => {
    setBusy(true);
    try {
      await api.seed({
        count,
        store_id: "BTMLayout",
        shadow_mode: shadow,
        demo_speed: speed,
        include_rte: true,
      });
      setRuns(await api.listRuns());
    } finally {
      setBusy(false);
    }
  };

  return (
    <div>
      <div className="card">
        <h3>Seed demo runs</h3>
        <p className="muted">
          Starts one durable Temporal workflow per perishable batch. With a high
          clock-speed a full 13-hour selling day replays in ~30s.
        </p>
        <div className="row">
          <label>
            Batches
            <input
              type="number"
              min={1}
              max={6}
              value={count}
              onChange={(e) => setCount(+e.target.value)}
            />
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
          <button onClick={seed} disabled={busy}>
            {busy ? "Seeding…" : "Seed runs"}
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
