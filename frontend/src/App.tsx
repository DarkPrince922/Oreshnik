import { Navigate, Route, Routes } from "react-router-dom";

import AnimatedBackground from "./components/AnimatedBackground";
import Layout from "./components/Layout";
import ProtectedRoute from "./components/ProtectedRoute";
import { AuthProvider } from "./lib/auth";
import Account from "./pages/Account";
import Costs from "./pages/Costs";
import Dashboard from "./pages/Dashboard";
import LLMProfiles from "./pages/LLMProfiles";
import Login from "./pages/Login";
import NewScan from "./pages/NewScan";
import Ops from "./pages/Ops";
import Runners from "./pages/Runners";
import Schedules from "./pages/Schedules";
import ScanDetail from "./pages/ScanDetail";
import Scans from "./pages/Scans";
import Users from "./pages/Users";

export default function App() {
  return (
    <AuthProvider>
      <div className="relative min-h-screen">
        <AnimatedBackground />
        <Routes>
          <Route path="/login" element={<Login />} />
          <Route
            element={
              <ProtectedRoute>
                <Layout />
              </ProtectedRoute>
            }
          >
            <Route index element={<Dashboard />} />
            <Route path="scans" element={<Scans />} />
            <Route
              path="scans/new"
              element={
                <ProtectedRoute roles={["admin", "operator"]}>
                  <NewScan />
                </ProtectedRoute>
              }
            />
            <Route path="scans/:id" element={<ScanDetail />} />
            <Route
              path="schedules"
              element={
                <ProtectedRoute roles={["admin", "operator"]}>
                  <Schedules />
                </ProtectedRoute>
              }
            />
            <Route
              path="costs"
              element={
                <ProtectedRoute roles={["admin", "operator"]}>
                  <Costs />
                </ProtectedRoute>
              }
            />
            <Route
              path="llm"
              element={
                <ProtectedRoute roles={["admin", "operator"]}>
                  <LLMProfiles />
                </ProtectedRoute>
              }
            />
            <Route path="runners" element={<Runners />} />
            <Route
              path="ops"
              element={
                <ProtectedRoute roles={["admin"]}>
                  <Ops />
                </ProtectedRoute>
              }
            />
            <Route
              path="users"
              element={
                <ProtectedRoute roles={["admin"]}>
                  <Users />
                </ProtectedRoute>
              }
            />
            <Route path="account" element={<Account />} />
          </Route>
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </div>
    </AuthProvider>
  );
}
