import { useEffect, useState } from "react";
import { api } from "../api";
import type { OfferOutcome } from "../types";
const DEFAULT_STORE_ID = "BZID-1304298141";

const fmt = (n: number) => `₹${(+n).toFixed(n % 1 ? 2 : 0)}`;
const pct = (n: number) => `${n >= 0 ? "+" : ""}${n.toFixed(1)}%`;

export default function MyOffers() {
  const [storeId, setStoreId] = useState(DEFAULT_STORE_ID);
  const [outcomes, setOutcomes] = useState<OfferOutcome[]>([]);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    setLoading(true);
    api
      .listStoreOffers(storeId)
      .then(setOutcomes)
      .finally(() => setLoading(false));
  }, [storeId]);

  return (
    <div>
      <div className="card">
        <div className="row spread">
          <h2>My Offers — before &amp; after</h2>
          <input
            value={storeId}
            onChange={(e) => setStoreId(e.target.value)}
            placeholder="Store ID"
            style={{ width: 140 }}
          />
        </div>
        <p className="muted">
          Each row shows the sell-through velocity before and after a discount was
          applied — proof that the markdown worked.
        </p>
      </div>

      {loading && <div className="card muted">Loading…</div>}

      {!loading && outcomes.length === 0 && (
        <div className="card muted">No offer outcomes recorded yet for this store.</div>
      )}

      {outcomes.length > 0 && (
        <div className="card" style={{ overflowX: "auto" }}>
          <table>
            <thead>
              <tr>
                <th>Product</th>
                <th>Rung</th>
                <th>Discount</th>
                <th>Rate before</th>
                <th>Rate after</th>
                <th>Lift</th>
                <th>Sold after</th>
                <th>Left</th>
                <th>Revenue saved</th>
                <th>Waste avoided</th>
                <th>Phase</th>
                <th>Time</th>
              </tr>
            </thead>
            <tbody>
              {outcomes.map((o, i) => (
                <tr key={i}>
                  <td>
                    <span title={o.jpin}>{o.product_title}</span>
                  </td>
                  <td>
                    <span className="tag">{o.headline || "—"}</span>
                  </td>
                  <td>{o.discount_pct.toFixed(0)}%</td>
                  <td>{o.rate_before.toFixed(1)}/h</td>
                  <td>{o.rate_after.toFixed(1)}/h</td>
                  <td>
                    <span
                      className={`chip ${o.lift_pct >= 0 ? "d-STEP" : "d-HOLD"}`}
                    >
                      {pct(o.lift_pct)}
                    </span>
                  </td>
                  <td>{o.units_sold_after}</td>
                  <td>{o.units_left}</td>
                  <td>{fmt(o.revenue_recovered)}</td>
                  <td>
                    {o.waste_avoided_units} u / {fmt(o.waste_avoided_value)}
                  </td>
                  <td className="muted">{o.phase}</td>
                  <td className="muted" style={{ whiteSpace: "nowrap" }}>
                    {new Date(o.ts_ist).toLocaleTimeString()}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
