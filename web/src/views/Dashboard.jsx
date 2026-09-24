import React, { useEffect, useState } from "react";
import { api } from "../api.js";

const fmt = (n) => (n ?? 0).toLocaleString();

// The dashboard's whole reason to exist is the stopped-line card: it shows the
// two losses side by side and adds them, so the perishable-WIP number a
// dashboard usually hides is right next to the throughput number. Everything
// else is read-only plant context.
export default function Dashboard({ principalKey }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    setData(null);
    setError(null);
    api.dashboard().then(setData).catch((e) => setError(e.message));
  }, [principalKey]);

  if (error) return <div className="banner err">{error}</div>;
  if (!data) return <div className="loading">Loading plant state…</div>;

  return (
    <div>
      <div className="view-title">Plant dashboard — {data.plant}</div>
      <div className="view-sub">
        Live read-only view. No model involved — this is the plant as the
        agents' tools see it.
      </div>

      {data.open_downtime.length === 0 ? (
        <div className="card">
          <span className="pill accent">All lines running</span>
        </div>
      ) : (
        data.open_downtime.map((s) => (
          <div key={s.line_id} className="card stopped">
            <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
              <h3>
                {s.plant}/{s.line} stopped
              </h3>
              <span className="pill danger">{s.reason}</span>
              <span className="pill">
                down {s.minutes_down}m · ETA {s.eta_minutes}m
              </span>
            </div>
            {s.detail && (
              <div className="muted" style={{ marginTop: 6 }}>
                {s.detail}
              </div>
            )}
            <div className="loss-row">
              <div className="loss">
                <div className="n">{fmt(s.throughput_loss_units)}</div>
                <div className="l">Output not produced</div>
              </div>
              <div className="plus">+</div>
              <div className="loss">
                <div className="n scrap">{fmt(s.wip_scrap_units)}</div>
                <div className="l">Dough that will over-proof</div>
              </div>
              <div className="plus">=</div>
              <div className="loss">
                <div className="n total">{fmt(s.total_at_risk_units)}</div>
                <div className="l">Total units at risk</div>
              </div>
            </div>
            {s.wip_batches.length > 0 && (
              <table>
                <thead>
                  <tr>
                    <th>Staged batch</th>
                    <th>SKU</th>
                    <th className="num">Units</th>
                    <th className="num">Min to expiry</th>
                    <th>Fate</th>
                  </tr>
                </thead>
                <tbody>
                  {s.wip_batches.map((b) => (
                    <tr key={b.batch_code}>
                      <td>{b.batch_code}</td>
                      <td>{b.sku_name}</td>
                      <td className="num">{fmt(b.units)}</td>
                      <td className="num">{b.minutes_until_expiry}</td>
                      <td>
                        {b.expires_before_restart ? (
                          <span className="pill danger">scrapped</span>
                        ) : (
                          <span className="pill accent">survives</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        ))
      )}

      <div className="card">
        <h3>Orders at risk (next 24h)</h3>
        {data.at_risk_orders.length === 0 ? (
          <div className="muted">None.</div>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Order</th>
                <th>Customer</th>
                <th>Tier</th>
                <th className="num">Units short</th>
                <th className="num">Ships in (h)</th>
              </tr>
            </thead>
            <tbody>
              {data.at_risk_orders.slice(0, 8).map((o, i) => (
                <tr key={i}>
                  <td>{o.order_code}</td>
                  <td>{o.customer}</td>
                  <td>
                    <span
                      className={
                        "pill " +
                        (o.priority_tier === "strategic" ? "danger" : "")
                      }
                    >
                      {o.priority_tier}
                    </span>
                  </td>
                  <td className="num">{fmt(o.units_outstanding)}</td>
                  <td className="num">{o.hours_until_ship}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div className="card">
        <h3>Scrap by line (30 days)</h3>
        <table>
          <thead>
            <tr>
              <th>Line</th>
              <th className="num">Scrap %</th>
              <th className="num">Scrap units</th>
            </tr>
          </thead>
          <tbody>
            {data.scrap_by_line.slice(0, 6).map((r, i) => (
              <tr key={i}>
                <td>{r.line}</td>
                <td className="num">{r.scrap_pct}%</td>
                <td className="num">{fmt(r.scrap_units)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
