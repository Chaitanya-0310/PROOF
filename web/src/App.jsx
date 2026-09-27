import React, { useEffect, useState } from "react";
import TopBar from "./components/TopBar.jsx";
import Chat from "./views/Chat.jsx";
import Dashboard from "./views/Dashboard.jsx";
import { api, getPrincipal, setPrincipal } from "./api.js";

const TABS = [
  { id: "dashboard", label: "Dashboard", icon: "◉" },
  { id: "chat", label: "Agent", icon: "◈" },
];

export default function App() {
  const [principals, setPrincipals] = useState([]);
  const [current, setCurrent] = useState(getPrincipal());
  const [tab, setTab] = useState("dashboard");
  const [fatal, setFatal] = useState(null);

  useEffect(() => {
    api.principals().then(setPrincipals).catch((e) => setFatal(e.message));
  }, []);

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
            <span style={{ marginRight: 6, fontSize: 10, opacity: 0.7 }}>
              {t.icon}
            </span>
            {t.label}
          </button>
        ))}
      </div>
      {/* Dashboard uses the standard wrap; Chat uses its own full-height layout */}
      {tab === "dashboard" && (
        <div className="wrap">
          <Dashboard principalKey={current} />
        </div>
      )}
      {tab === "chat" && <Chat principalKey={current} />}
    </>
  );
}
