import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { api, poll } from "../api";
import type { DeadStockRunDetail as Detail } from "../types";

const fmt = (n: number) => `₹${(+n).toFixed(n % 1 ? 2 : 0)}`;

export default function DeadStockRun() {
  const { id = "" } = useParams();
  const [run, setRun] = useState<Detail | null>(null);
  const [simOnHand, setSimOnHand] = useState("");
  const [simMsg, setSimMsg] = useState("");

  useEffect(() => poll(() => api.getDeadStockRun(id), 1500, setRun), [id]);
  if (!run) return <div className="card">Loading…</div>;
  const live = run.live;
  const price = live?.current_price ?? run.current_price;
  const onHand = live?.on_hand ?? run.on_hand;
  const dte = live?.days_to_expiry ?? run.days_to_expiry;

  const decide = (approve: boolean) =>
    api.decide(id, approve, "R2").then(() => api.getDeadStockRun(id).then(setRun));

  const applySim = () => {
    if (simOnHand === "") {
      setSimMsg("Enter an on-hand quantity.");
      return;
    }
    api.simulate(id, { q0: parseInt(simOnHand, 10) }).then(() => {
      setSimMsg("Applied — the agent re-decides on the next day tick.");
      api.getDeadStockRun(id).then(setRun);
    });
  };

  return (
    <div>
      <div className="card">
        <div className="row spread">
          <div>
            <h3>{run.product_title || run.jpin}</h3>
            <div className="muted">{run.run_id}</div>
          </div>
          <span className={`chip s-${run.status}`}>{run.status}</span>
        </div>
        <div className="stats">
          <Stat label="Current price" value={fmt(price)} sub={`list ${fmt(run.list_price)}`} />
          <Stat label="Discount" value={`${(live?.current_discount_pct ?? run.current_discount_pct).toFixed(0)}%`} />
          <Stat label="On hand" value={String(onHand)} />
          <Stat label="Days to expiry" value={String(dte)} sub={`shelf ${run.shelf_life_days}d`} />
          <Stat label="Unsold" value={`${live?.days_unsold ?? run.days_unsold}d`} />
          <Stat label="Mode" value={live?.mode ?? run.mode}
            sub={(live?.reorder_action ?? run.reorder_action) !== "NONE" ? (live?.reorder_action ?? run.reorder_action) : undefined} />
        </div>
        <p className="reason">{live?.last_reason || run.summary}</p>
      </div>

      {run.awaiting_approval && (
        <div className="card approval">
          <h3>Owner approval needed</h3>
          <p>
            Mark down <b>{run.product_title || run.jpin}</b> to{" "}
            <b>{fmt(live?.pending_price ?? 0)}</b> — {onHand} on hand, {dte}d to expiry.
          </p>
          <div className="row">
            <button className="ok" onClick={() => decide(true)}>Approve</button>
            <button className="bad" onClick={() => decide(false)}>Reject</button>
          </div>
        </div>
      )}

      <div className="card">
        <h3>Steering</h3>
        <div className="row">
          <button onClick={() => api.soldOut(id)}>Mark sold out</button>
          <button onClick={() => api.override(id, "stop")}>Stop run</button>
        </div>
        {run.simulate && (
          <div className="row" style={{ marginTop: 12, alignItems: "center" }}>
            <label style={{ marginRight: 6 }}>Simulate on-hand</label>
            <input type="number" min={0} placeholder={String(onHand)} value={simOnHand}
              onChange={(e) => setSimOnHand(e.target.value)} style={{ width: 90, marginRight: 6 }} />
            <button onClick={applySim}>Apply</button>
            {simMsg && <span className="muted" style={{ marginLeft: 8 }}>{simMsg}</span>}
          </div>
        )}
      </div>

      <div className="card">
        <h3>Reason trail</h3>
        <table>
          <thead>
            <tr><th>Decision</th><th>Price</th><th>Reason</th></tr>
          </thead>
          <tbody>
            {run.decisions.map((d, i) => (
              <tr key={i}>
                <td><span className={`chip d-${d.decision}`}>{d.decision}</span></td>
                <td>{fmt(d.price)}</td>
                <td className="reason-cell">{d.reason}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="card">
        <h3>Price ledger</h3>
        {run.price_changes.length === 0 && <p className="muted">No applied prices.</p>}
        {run.price_changes.map((p, i) => (
          <div key={i} className="line">
            #{p.price_seq} {p.rung}: {fmt(p.from_price)} → <b>{fmt(p.to_price)}</b>{" "}
            {p.confirmed && <span className="tag ok">confirmed</span>}
          </div>
        ))}
      </div>

      <div className="card">
        <h3>Event timeline</h3>
        <ul className="timeline">
          {run.events.map((e, i) => (
            <li key={i}><span className="tag">{e.kind}</span> {e.message}</li>
          ))}
        </ul>
      </div>
    </div>
  );
}

function Stat({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="stat">
      <div className="stat-label">{label}</div>
      <div className="stat-value">{value}</div>
      {sub && <div className="stat-sub">{sub}</div>}
    </div>
  );
}
