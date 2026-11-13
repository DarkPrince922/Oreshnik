import { Navigate, Route, Routes } from "react-router-dom";

import AnimatedBackground from "./components/AnimatedBackground";
import Layout from "./components/Layout";
import ProtectedRoute from "./components/ProtectedRoute";
import { AuthProvider } from "./lib/auth";
import Account from "./pages/Account";
import Dashboard from "./pages/Dashboard";
import LLMProfiles from "./pages/LLMProfiles";
import Login from "./pages/Login";
import Runners from "./pages/Runners";
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
