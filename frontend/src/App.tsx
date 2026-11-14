import { Navigate, Route, Routes } from "react-router-dom";
import { DashboardLayout } from "./components/DashboardLayout";
import { Analyze } from "./pages/Analyze";
import { Channel } from "./pages/Channel";
import { Claims } from "./pages/Claims";
import { Landing } from "./pages/Landing";
import { Narratives } from "./pages/Narratives";
import { NotFound } from "./pages/NotFound";
import { Sources } from "./pages/Sources";
import { StoryTour } from "./pages/StoryTour";
import { System } from "./pages/System";

/** All routes. Rendered inside a router by main.tsx (browser) or tests (memory). */
export function AppRoutes() {
  return (
    <Routes>
      <Route path="/" element={<Landing />} />
      <Route path="/story/:id" element={<StoryTour />} />
      <Route path="/dashboard" element={<DashboardLayout />}>
        <Route index element={<Navigate to="narratives" replace />} />
        <Route path="narratives" element={<Narratives />} />
        <Route path="analyze" element={<Analyze />} />
        <Route path="claims" element={<Claims />} />
        <Route path="sources" element={<Sources />} />
        <Route path="sources/:id" element={<Channel />} />
        <Route path="system" element={<System />} />
        <Route path="*" element={<NotFound />} />
      </Route>
      <Route path="*" element={<NotFound />} />
    </Routes>
  );
}
