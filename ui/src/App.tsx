import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import { Layout } from './components/Layout.tsx'
import { LiveProvider } from './lib/live.tsx'
import { CaseFile } from './pages/CaseFile.tsx'
import { Cases } from './pages/Cases.tsx'
import { ControlRoom } from './pages/ControlRoom.tsx'
import { Pool } from './pages/Pool.tsx'
import { Scorecard } from './pages/Scorecard.tsx'
import { Tenants } from './pages/Tenants.tsx'

export default function App() {
  return (
    <LiveProvider>
      <BrowserRouter>
        <Routes>
          <Route element={<Layout />}>
            <Route index element={<ControlRoom />} />
            <Route path="pool" element={<Pool />} />
            <Route path="tenants" element={<Tenants />} />
            <Route path="case" element={<Cases />} />
            <Route path="case/:incidentId" element={<CaseFile />} />
            <Route path="scorecard" element={<Scorecard />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </LiveProvider>
  )
}
