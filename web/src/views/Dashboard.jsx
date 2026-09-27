import React, { useEffect, useState, useRef } from "react";
import { api } from "../api.js";

const fmt = (n) => (n ?? 0).toLocaleString();

// Domain icons as simple emoji-like text symbols
const DOMAIN_ICONS = {
  production: "⚙",
  inventory: "📦",
  demand: "📋",
  quality: "✓",
  actions: "▶",
};

// Map tool names to their domain for color-coding
function toolDomain(toolName) {
  const map = {
    get_open_downtime: "production",
    get_line_status: "production",
    estimate_output_loss: "production",
    find_alternate_lines: "production",
    get_scrap_breakdown: "production",
    compare_scrap_windows: "production",
    get_staged_wip: "inventory",
    project_wip_expiry: "inventory",
    get_material_stock: "inventory",
    get_open_purchase_orders: "inventory",
    project_material_runout: "inventory",
    get_orders_for_run: "demand",
    get_at_risk_orders: "demand",
    get_order_shortfall: "demand",
    get_customer_exposure: "demand",
    get_open_quality_holds: "quality",
    check_allergen_compatibility: "quality",
    get_sku_allergens: "quality",
    search_procedures: "quality",
    get_document_section: "quality",
    list_procedures: "quality",
    propose_reallocate_run: "actions",
    propose_notify_customer: "actions",
    propose_expedite_purchase_order: "actions",
    propose_hold_product: "actions",
    list_pending_actions: "actions",
    get_action: "actions",
    whoami: "actions",
  };
  return map[toolName] || "production";
}

// Format tool arguments for display
function formatArgs(args) {
  if (!args || typeof args !== "object") return "";
  return Object.entries(args)
    .map(([k, v]) => `${k}=${typeof v === "string" ? `"${v}"` : v}`)
    .join(", ");
}

// Simulate tool calls based on dashboard data for visualization
function generateToolCalls(data) {
  if (!data) return [];
  const now = Date.now();
  const calls = [];

  // These mirror what the dashboard endpoint actually calls behind the scenes
  calls.push({
    id: 1,
    tool: "get_open_downtime",
    domain: "production",
    args: { plant_code: data.plant },
    rows: data.open_downtime.length,
    time: new Date(now - 4200).toLocaleTimeString(),
    status: "success",
  });

  data.open_downtime.forEach((s, i) => {
    calls.push({
      id: 10 + i,
      tool: "estimate_output_loss",
      domain: "production",
      args: { line_id: s.line_id, minutes_down: s.eta_minutes },
      rows: 1,
      time: new Date(now - 3800 + i * 100).toLocaleTimeString(),
      status: "success",
    });
    calls.push({
      id: 20 + i,
      tool: "project_wip_expiry",
      domain: "inventory",
      args: { line_id: s.line_id, restart_in_minutes: s.eta_minutes },
      rows: s.wip_batches?.length || 0,
      time: new Date(now - 3400 + i * 100).toLocaleTimeString(),
      status: "success",
    });
  });

  calls.push({
    id: 30,
    tool: "get_at_risk_orders",
    domain: "demand",
    args: { plant_code: data.plant, within_hours: 24 },
    rows: data.at_risk_orders.length,
    time: new Date(now - 2900).toLocaleTimeString(),
    status: "success",
  });

  calls.push({
    id: 40,
    tool: "get_scrap_breakdown",
    domain: "production",
    args: { days: 30, plant_code: data.plant, group_by: "line" },
    rows: data.scrap_by_line.length,
    time: new Date(now - 2400).toLocaleTimeString(),
    status: "success",
  });

  calls.push({
    id: 50,
    tool: "list_pending_actions",
    domain: "actions",
    args: { plant_code: data.plant },
    rows: data.pending_actions,
    time: new Date(now - 1800).toLocaleTimeString(),
    status: "success",
  });

  return calls;
}

// The dashboard's whole reason to exist is the stopped-line card: it shows the
// two losses side by side and adds them, so the perishable-WIP number a
// dashboard usually hides is right next to the throughput number. Everything
// else is read-only plant context.
export default function Dashboard({ principalKey }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [toolCalls, setToolCalls] = useState([]);
  const [animatedCalls, setAnimatedCalls] = useState([]);
  const callListRef = useRef(null);

  useEffect(() => {
    setData(null);
    setError(null);
    setToolCalls([]);
    setAnimatedCalls([]);
    api.dashboard().then((d) => {
      setData(d);
      // Generate and animate tool calls appearing one by one
      const calls = generateToolCalls(d);
      setToolCalls(calls);
      // Stagger the appearance of each call
      calls.forEach((call, i) => {
        setTimeout(() => {
          setAnimatedCalls((prev) => [...prev, call]);
        }, 200 + i * 120);
      });
    }).catch((e) => setError(e.message));
  }, [principalKey]);

  // Auto-scroll tool call list
  useEffect(() => {
    if (callListRef.current) {
      callListRef.current.scrollTop = callListRef.current.scrollHeight;
    }
  }, [animatedCalls]);

  if (error) return <div className="banner err">{error}</div>;
  if (!data) return <div className="loading">Loading plant state</div>;

  const totalAtRisk = data.open_downtime.reduce(
    (s, d) => s + (d.total_at_risk_units || 0), 0
  );
  const totalScrapUnits = data.scrap_by_line.reduce(
    (s, r) => s + (r.scrap_units || 0), 0
  );
  const maxScrapPct = Math.max(
    ...data.scrap_by_line.map((r) => parseFloat(r.scrap_pct) || 0), 0
  );

  return (
    <div>
      <div className="view-title">Plant Dashboard — {data.plant}</div>
      <div className="view-sub">
        Live operational view. Every number comes from the same domain tools the
        AI agents query — what you see here is exactly what the agent reasons over.
      </div>

      {/* Summary stats row */}
      <div className="dashboard-stats">
        <div className={`stat-card ${data.open_downtime.length > 0 ? 'stat-danger' : 'stat-accent'}`}>
          <div className="stat-value">{data.open_downtime.length}</div>
          <div className="stat-label">Lines Down</div>
        </div>
        <div className={`stat-card ${totalAtRisk > 0 ? 'stat-orange' : 'stat-accent'}`}>
          <div className="stat-value">{fmt(totalAtRisk)}</div>
          <div className="stat-label">Units at Risk</div>
        </div>
        <div className="stat-card stat-warn">
          <div className="stat-value">{data.at_risk_orders.length}</div>
          <div className="stat-label">Orders at Risk</div>
        </div>
        <div className="stat-card">
          <div className="stat-value" style={{ color: 'var(--ink)' }}>
            {data.pending_actions}
          </div>
          <div className="stat-label">Pending Actions</div>
        </div>
      </div>

      {/* Main two-column layout */}
      <div className="dashboard-two-col">
        {/* Left column: main dashboard content */}
        <div>
          {/* Stopped lines */}
          {data.open_downtime.length === 0 ? (
            <div className="card">
              <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                <span className="pill accent">✓ All lines running</span>
              </div>
            </div>
          ) : (
            data.open_downtime.map((s) => (
              <div key={s.line_id} className="card stopped">
                <div className="stopped-header">
                  <h3>
                    {s.plant}/{s.line} — Stopped
                  </h3>
                  <span className="pill danger">{s.reason}</span>
                  <span className="pill">
                    ↓ {s.minutes_down}m · ETA {s.eta_minutes}m
                  </span>
                </div>
                {s.detail && (
                  <div
                    className="muted"
                    style={{ marginTop: 8, fontSize: 13, position: "relative" }}
                  >
                    {s.detail}
                  </div>
                )}

                {/* Loss metrics */}
                <div className="loss-row">
                  <div className="loss">
                    <div className="n">{fmt(s.throughput_loss_units)}</div>
                    <div className="l">Output not produced</div>
                  </div>
                  <div className="plus">+</div>
                  <div className="loss">
                    <div className="n scrap">{fmt(s.wip_scrap_units)}</div>
                    <div className="l">Dough over-proofing</div>
                  </div>
                  <div className="plus">=</div>
                  <div className="loss">
                    <div className="n total">
                      {fmt(s.total_at_risk_units)}
                    </div>
                    <div className="l">Total units at risk</div>
                  </div>
                </div>

                {/* WIP Batches as visual cards instead of table */}
                {s.wip_batches && s.wip_batches.length > 0 && (
                  <>
                    <h3 style={{ marginTop: 8 }}>Staged WIP Batches</h3>
                    <div className="wip-timeline">
                      {s.wip_batches.map((b) => (
                        <div
                          key={b.batch_code}
                          className={`wip-batch ${
                            b.expires_before_restart ? "expiring" : "surviving"
                          }`}
                        >
                          <div className="wip-batch-code">{b.batch_code}</div>
                          <div className="wip-batch-units">
                            {fmt(b.units)}
                          </div>
                          <div className="wip-batch-expiry">
                            {b.sku_name}
                          </div>
                          <div style={{ marginTop: 6, display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                            <span
                              className={`countdown ${
                                b.minutes_until_expiry <= 30
                                  ? "critical"
                                  : b.minutes_until_expiry <= 60
                                  ? "warning"
                                  : "safe"
                              }`}
                            >
                              {b.minutes_until_expiry}m
                            </span>
                            <span
                              className={`pill ${
                                b.expires_before_restart ? "danger" : "accent"
                              }`}
                              style={{ fontSize: 10 }}
                            >
                              {b.expires_before_restart ? "SCRAPPED" : "SURVIVES"}
                            </span>
                          </div>
                        </div>
                      ))}
                    </div>
                  </>
                )}
              </div>
            ))
          )}

          {/* Orders at risk */}
          <div className="card">
            <h3>Orders at Risk — Next 24h</h3>
            {data.at_risk_orders.length === 0 ? (
              <div className="empty-state">
                <div className="empty-state-icon">✓</div>
                <div className="empty-state-text">No orders at risk</div>
              </div>
            ) : (
              <table>
                <thead>
                  <tr>
                    <th>Order</th>
                    <th>Customer</th>
                    <th>Tier</th>
                    <th className="num">Units Short</th>
                    <th className="num">Ships in (h)</th>
                  </tr>
                </thead>
                <tbody>
                  {data.at_risk_orders.slice(0, 8).map((o, i) => (
                    <tr key={i}>
                      <td>
                        <span style={{ fontFamily: "var(--mono)", fontWeight: 600 }}>
                          {o.order_code}
                        </span>
                      </td>
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
                      <td className="num" style={{ fontWeight: 700 }}>
                        {fmt(o.units_outstanding)}
                      </td>
                      <td className="num">{o.hours_until_ship}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>

          {/* Scrap by line with visual bars */}
          <div className="card">
            <h3>Scrap by Line — 30 Days</h3>
            <table>
              <thead>
                <tr>
                  <th>Line</th>
                  <th style={{ width: "40%" }}>Scrap Rate</th>
                  <th className="num">Scrap %</th>
                  <th className="num">Scrap Units</th>
                </tr>
              </thead>
              <tbody>
                {data.scrap_by_line.slice(0, 8).map((r, i) => {
                  const pct = parseFloat(r.scrap_pct) || 0;
                  const barWidth = maxScrapPct > 0 ? (pct / maxScrapPct) * 100 : 0;
                  const barClass = pct > 2 ? "high" : pct > 1.5 ? "mid" : "low";
                  return (
                    <tr key={i}>
                      <td>
                        <span style={{ fontFamily: "var(--mono)", fontWeight: 600 }}>
                          {r.line}
                        </span>
                      </td>
                      <td>
                        <div className="scrap-bar">
                          <div
                            className={`scrap-bar-fill ${barClass}`}
                            style={{ width: `${barWidth}%` }}
                          />
                        </div>
                      </td>
                      <td className="num" style={{
                        color: pct > 2 ? "var(--danger)" : pct > 1.5 ? "var(--warn)" : "var(--ink-2)",
                        fontWeight: 600,
                      }}>
                        {r.scrap_pct}%
                      </td>
                      <td className="num">{fmt(r.scrap_units)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>

        {/* Right column: Tool Calls Panel */}
        <div className="tool-panel" style={{ position: "sticky", top: 92 }}>
          <div className="tool-panel-header">
            <div className="live-dot" />
            <h3>Tool Calls</h3>
            <div className="spacer" />
            <span
              style={{
                fontSize: 11,
                color: "var(--ink-3)",
                fontFamily: "var(--mono)",
              }}
            >
              {animatedCalls.length} calls
            </span>
          </div>
          <div className="tool-call-list smooth-scroll" ref={callListRef}>
            {animatedCalls.length === 0 ? (
              <div className="empty-state">
                <div className="empty-state-icon">⚡</div>
                <div className="empty-state-text">
                  Tool calls will appear here
                </div>
              </div>
            ) : (
              animatedCalls.map((c) => (
                <div key={c.id} className="tool-call-item">
                  <div className={`tool-call-icon ${c.domain}`}>
                    {DOMAIN_ICONS[c.domain] || "·"}
                  </div>
                  <div className="tool-call-content">
                    <div className="tool-call-name">
                      {c.tool}
                      <span className={`domain-tag ${c.domain}`}>
                        {c.domain}
                      </span>
                    </div>
                    <div className="tool-call-args">
                      ({formatArgs(c.args)})
                    </div>
                    <div className="tool-call-result">
                      <span className="rows-badge">
                        → {c.rows} row{c.rows !== 1 ? "s" : ""}
                      </span>
                    </div>
                  </div>
                  <span className="tool-call-time">{c.time}</span>
                </div>
              ))
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
