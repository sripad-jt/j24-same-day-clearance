import React from "react";
import ReactDOM from "react-dom/client";
import { createBrowserRouter, RouterProvider } from "react-router-dom";
import App from "./App";
import Dashboard from "./pages/Dashboard";
import RunDetail from "./pages/RunDetail";
import Approval from "./pages/Approval";
import Config from "./pages/Config";
import MyOffers from "./pages/MyOffers";
import "./styles.css";

const router = createBrowserRouter(
  [
    {
      path: "/",
      element: <App />,
      children: [
        { index: true, element: <Dashboard /> },
        { path: "approvals", element: <Approval /> },
        { path: "config", element: <Config /> },
        { path: "runs/:id", element: <RunDetail /> },
        { path: "offers", element: <MyOffers /> },
      ],
    },
  ],
  { basename: import.meta.env.BASE_URL.replace(/\/$/, "") }
);

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <RouterProvider router={router} />
  </React.StrictMode>
);
