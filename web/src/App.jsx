import React, { useEffect, useState } from "react";
import TopBar from "./components/TopBar.jsx";
import Ask from "./views/Ask.jsx";
import Dashboard from "./views/Dashboard.jsx";
import Approvals from "./views/Approvals.jsx";
import { api, getPrincipal, setPrincipal } from "./api.js";

const TABS = [
  { id: "ask", label: "Ask" },
  { id: "dashboard", label: "Dashboard" },
  { id: "approvals", label: "Approvals" },
];

export default function App() {
  const [principals, setPrincipals] = useState([]);
  const [current, setCurrent] = useState(getPrincipal());
  const [tab, setTab] = useState("dashboard");
  const [pendingCount, setPendingCount] = useState(null);
  const [fatal, setFatal] = useState(null);

  useEffect(() => {
    api.principals().then(setPrincipals).catch((e) => setFatal(e.message));
  }, []);

  // Re-read the pending count whenever the acting principal or tab changes, so
  // the Approvals tab badge reflects the current scope.
  useEffect(() => {
    api
      .pending()
      .then((d) => setPendingCount(d.rows.length))
      .catch(() => setPendingCount(null));
  }, [current, tab]);

  function switchPrincipal(id) {
    setPrincipal(id);
    setCurrent(id);
  }

  if (fatal) {
    return (
      <div className="wrap">
        <div className="banner err">
          Can't reach the API: {fatal}. Is the FastAPI server running on
          :8000? Start it with{" "}
          <code>uv run uvicorn proof.api.app:app --port 8000</code>.
        </div>
      </div>
    );
  }

  return (
    <>
      <TopBar
        principals={principals}
        current={current}
        onSwitch={switchPrincipal}
      />
      <div className="tabs">
        {TABS.map((t) => (
          <button
            key={t.id}
            className={"tab" + (tab === t.id ? " active" : "")}
            onClick={() => setTab(t.id)}
          >
            {t.label}
            {t.id === "approvals" && pendingCount > 0 && (
              <span className="pill">{pendingCount}</span>
            )}
          </button>
        ))}
      </div>
      <div className="wrap">
        {/* principalKey forces each view to reload when the acting user changes */}
        {tab === "ask" && <Ask principalKey={current} />}
        {tab === "dashboard" && <Dashboard principalKey={current} />}
        {tab === "approvals" && <Approvals principalKey={current} />}
      </div>
    </>
  );
}
