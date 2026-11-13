import { Navigate, Route, Routes } from "react-router-dom";

import AnimatedBackground from "./components/AnimatedBackground";
import Login from "./pages/Login";

export default function App() {
  return (
    <div className="relative min-h-screen">
      <AnimatedBackground />
      <Routes>
        <Route path="/login" element={<Login />} />
        {/* Phase 0: everything routes to login until auth + dashboard land. */}
        <Route path="*" element={<Navigate to="/login" replace />} />
      </Routes>
    </div>
  );
}
