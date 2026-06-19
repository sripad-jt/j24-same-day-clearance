import { useEffect, useState } from "react";
import { api } from "../api";

export default function Config() {
  const [cfg, setCfg] = useState<any>(null);
  useEffect(() => {
    api.getConfig().then(setCfg).catch(() => {});
  }, []);
  if (!cfg) return <div className="card">Loading…</div>;

  return (
    <div>
      <div className="card">
        <h3>Markdown ladder</h3>
        <p className="muted">
          Snapshotted into each run at start (kept in config, not code — ops can
          re-tune without a deploy).
        </p>
        <table>
          <thead>
            <tr>
              <th>Rung</th>
              <th>Elapsed trigger</th>
              <th>Wall-clock (IST)</th>
              <th>Ceiling</th>
              <th>Behaviour</th>
            </tr>
          </thead>
          <tbody>
            {cfg.rungs.map((r: any) => (
              <tr key={r.label}>
                <td>{r.label}</td>
                <td>{r.elapsed_hours != null ? `T0 + ${r.elapsed_hours}h` : "—"}</td>
                <td>{r.wallclock_hour_ist != null ? `${r.wallclock_hour_ist}:00` : "—"}</td>
                <td>{r.token_free ? "₹1 token" : `${r.ceiling_pct}%`}</td>
                <td>{r.token_free ? "clearance gesture" : r.index === 0 ? "observe" : "markdown"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="card">
        <h3>Thresholds</h3>
        <div className="stats">
          <KV k="theta_hold" v={cfg.theta_hold} />
          <KV k="trailing window (h)" v={cfg.trailing_window_hours} />
          <KV k="min Q0 to run" v={cfg.min_q0} />
          <KV k="approval timeout (min)" v={cfg.approval_timeout_minutes} />
          <KV k="RTE auto-clear gate (IST)" v={`${cfg.rte_autoclear_gate_hour}:00`} />
          <KV k="store close (IST)" v={`${cfg.store_close_hour}:00`} />
          <KV k="token price" v={`₹${cfg.token_free_price}`} />
        </div>
      </div>
    </div>
  );
}

function KV({ k, v }: { k: string; v: any }) {
  return (
    <div className="stat">
      <div className="stat-label">{k}</div>
      <div className="stat-value">{String(v)}</div>
    </div>
  );
}
