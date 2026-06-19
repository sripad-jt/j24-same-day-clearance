import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, poll } from "../api";
import type { RunSummary } from "../types";

const fmt = (n: number) => `₹${(+n).toFixed(n % 1 ? 2 : 0)}`;

export default function Approval() {
  const [runs, setRuns] = useState<RunSummary[]>([]);
  useEffect(() => poll(api.listRuns, 1500, setRuns), []);

  const awaiting = runs.filter((r) => r.awaiting_approval);

  const decide = async (id: string, rung: string, approve: boolean) => {
    await api.decide(id, approve, rung);
    setRuns(await api.listRuns());
  };

  return (
    <div>
      <div className="card">
        <h3>Clearance offers awaiting your nod ({awaiting.length})</h3>
        <p className="muted">
          One tap, like clearing an email. No response within the window holds at
          the current price.
        </p>
      </div>
      {awaiting.length === 0 && (
        <div className="card muted">Nothing waiting. The agent is observing.</div>
      )}
      {awaiting.map((r) => (
        <div key={r.run_id} className="card approval">
          <div className="row spread">
            <h3>
              {r.product_title} {r.is_rte && <span className="tag rte">RTE</span>}
            </h3>
            <span className="chip await">{r.current_rung}</span>
          </div>
          <p>
            {Math.max(0, r.q0 - r.units_sold)} of {r.q0} units left ·{" "}
            current {fmt(r.current_price)}
          </p>
          <p className="reason">{r.summary}</p>
          <div className="row">
            <button className="ok" onClick={() => decide(r.run_id, r.current_rung, true)}>
              Approve markdown
            </button>
            <button className="bad" onClick={() => decide(r.run_id, r.current_rung, false)}>
              Reject
            </button>
            <Link className="link" to={`/runs/${encodeURIComponent(r.run_id)}`}>
              details →
            </Link>
          </div>
        </div>
      ))}
    </div>
  );
}
