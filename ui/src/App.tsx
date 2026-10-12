import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import { Layout } from './components/Layout.tsx'
import { LiveProvider } from './lib/live.tsx'
import { ControlRoom } from './pages/ControlRoom.tsx'
import { Tenants } from './pages/Tenants.tsx'

export default function App() {
  return (
    <LiveProvider>
      <BrowserRouter>
        <Routes>
          <Route element={<Layout />}>
            <Route index element={<ControlRoom />} />
            <Route path="tenants" element={<Tenants />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </LiveProvider>
  )
}
