import React from "react";

// The "acting as" switcher stands in for signing in as different people. It
// is the demo's way of showing that authority is a property of WHO you are,
// not what you type -- switch from the supervisor to the regional director
// and the same approval that was refused now goes through.
export default function TopBar({ principals, current, onSwitch }) {
  const me = principals.find((p) => p.principal_id === current);
  return (
    <div className="topbar">
      <div className="brand">
        PROOF<small>operator console</small>
      </div>
      <div className="spacer" />
      <div className="who">
        <div>
          acting as{" "}
          <select value={current} onChange={(e) => onSwitch(e.target.value)}>
            {principals.map((p) => (
              <option key={p.principal_id} value={p.principal_id}>
                {p.display_name}
              </option>
            ))}
          </select>
        </div>
        {me && (
          <div className="muted" style={{ marginTop: 4 }}>
            {me.role.replace(/_/g, " ")} · {me.plant_scope.join(", ")}
          </div>
        )}
      </div>
    </div>
  );
}
