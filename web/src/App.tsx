import { useEffect, useState } from "react";
import { Link, NavLink, Outlet } from "react-router-dom";
import { api, poll } from "./api";

export default function App() {
  const [awaiting, setAwaiting] = useState(0);

  useEffect(
    () =>
      poll(api.listRuns, 2000, (runs) =>
        setAwaiting(runs.filter((r) => r.awaiting_approval).length)
      ),
    []
  );

  return (
    <div className="app">
      <header>
        <Link to="/" className="brand">
          🥬 J24 Same-Day Clearance
        </Link>
        <nav>
          <NavLink to="/" end>
            Dashboard
          </NavLink>
          <NavLink to="/approvals">
            Approvals{awaiting > 0 && <span className="badge">{awaiting}</span>}
          </NavLink>
          <NavLink to="/config">Config</NavLink>
        </nav>
      </header>
      <main>
        <Outlet />
      </main>
    </div>
  );
}
