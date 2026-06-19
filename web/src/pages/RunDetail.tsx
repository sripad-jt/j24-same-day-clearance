import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { api, poll } from "../api";
import type { RunDetail as Detail } from "../types";

const fmt = (n: number) => `₹${(+n).toFixed(n % 1 ? 2 : 0)}`;
const RUNGS = ["R0", "R1", "R2", "R3"];
const RUNG_LABEL: Record<string, string> = {
  R0: "List",
  R1: "25% off",
  R2: "50% off",
  R3: "₹1 token",
};

export default function RunDetail() {
  const { id = "" } = useParams();
  const [run, setRun] = useState<Detail | null>(null);

  useEffect(() => poll(() => api.getRun(id), 1500, setRun), [id]);

  if (!run) return <div className="card">Loading…</div>;
  const live = run.live;
  const rate = live?.run_rate ?? 0;
  const ratio = live?.ratio ?? 0;
  const proj = live?.projected_clearance ?? 0;
  const residual = live?.residual ?? 0;

  const decide = (approve: boolean) =>
    api.decide(id, approve, run.current_rung).then(() => api.getRun(id).then(setRun));

  return (
    <div>
      <div className="card">
        <div className="row spread">
          <div>
            <h3>
              {run.product_title} {run.is_rte && <span className="tag rte">RTE</span>}
            </h3>
            <div className="muted">{run.run_id}</div>
          </div>
          <span className={`chip s-${run.status}`}>{run.status}</span>
        </div>

        <div className="ladder">
          {RUNGS.map((rg) => {
            const active = run.current_rung === rg;
            const pending = live?.pending_rung === rg;
            const passed = RUNGS.indexOf(rg) <= RUNGS.indexOf(run.current_rung);
            return (
              <div
                key={rg}
                className={`rung ${passed ? "passed" : ""} ${active ? "active" : ""} ${
                  pending ? "pending" : ""
                }`}
              >
                <div className="rung-label">{rg}</div>
                <div className="rung-sub">{RUNG_LABEL[rg]}</div>
              </div>
            );
          })}
        </div>

        <div className="stats">
          <Stat label="Current price" value={fmt(run.current_price)} sub={`list ${fmt(run.list_price)}`} />
          <Stat label="Sold / Q0" value={`${run.units_sold} / ${run.q0}`} />
          <Stat label="Run rate" value={`${rate.toFixed(1)}/h`} />
          <Stat label="Projected" value={proj.toFixed(0)} sub={`ratio ${ratio.toFixed(2)}`} />
          <Stat label="Residual" value={residual.toFixed(0)} />
        </div>
        <p className="reason">{live?.last_reason || run.summary}</p>
      </div>

      {run.awaiting_approval && (
        <div className="card approval">
          <h3>Owner approval needed</h3>
          <p>
            Step <b>{run.product_title}</b> from {fmt(live?.current_price ?? run.current_price)} to{" "}
            <b>{fmt(live?.pending_price ?? 0)}</b> ({live?.pending_rung}) —{" "}
            {Math.max(0, run.q0 - run.units_sold)} units left.
          </p>
          <p className="reason">{live?.last_reason}</p>
          <div className="row">
            <button className="ok" onClick={() => decide(true)}>
              Approve
            </button>
            <button className="bad" onClick={() => decide(false)}>
              Reject
            </button>
          </div>
        </div>
      )}

      <div className="card">
        <h3>Steering</h3>
        <div className="row">
          <button onClick={() => api.soldOut(id)}>Mark sold out</button>
          <button onClick={() => api.override(id, "stop")}>Stop run</button>
          <button onClick={() => api.override(id, "force_rung", "R2")}>Force R2 (50%)</button>
          <button onClick={() => api.grn(id, 10)}>+10 GRN re-receipt</button>
        </div>
      </div>

      <div className="card">
        <h3>Reason trail</h3>
        <table>
          <thead>
            <tr>
              <th>Rung</th>
              <th>Decision</th>
              <th>Approval</th>
              <th>Sold</th>
              <th>Ratio</th>
              <th>Reason</th>
            </tr>
          </thead>
          <tbody>
            {run.decisions.map((d, i) => (
              <tr key={i}>
                <td>{d.rung}</td>
                <td>
                  <span className={`chip d-${d.decision}`}>{d.decision}</span>
                </td>
                <td>{d.approval}</td>
                <td>{d.units_sold}</td>
                <td>{d.ratio.toFixed(2)}</td>
                <td className="reason-cell">{d.reason}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="grid2">
        <div className="card">
          <h3>Price ledger</h3>
          {run.price_changes.length === 0 && <p className="muted">No applied prices.</p>}
          {run.price_changes.map((p, i) => (
            <div key={i} className="line">
              {p.rung}: {fmt(p.from_price)} → <b>{fmt(p.to_price)}</b>{" "}
              {p.confirmed && <span className="tag ok">confirmed</span>}
            </div>
          ))}
        </div>
        <div className="card">
          <h3>Published offers</h3>
          {run.offers.length === 0 && <p className="muted">No offers published.</p>}
          {run.offers.map((o, i) => (
            <div key={i} className="line">
              <b>{o.rung}</b> · {o.headline}
            </div>
          ))}
        </div>
      </div>

      <div className="card">
        <h3>Event timeline</h3>
        <ul className="timeline">
          {run.events.map((e, i) => (
            <li key={i}>
              <span className="tag">{e.kind}</span> {e.message}
            </li>
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
