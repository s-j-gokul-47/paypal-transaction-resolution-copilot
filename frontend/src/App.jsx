import React from 'react'
import { Routes, Route, Navigate } from 'react-router-dom'
import Sidebar from './components/Sidebar'
import Dashboard from './pages/Dashboard'
import TransactionList from './pages/TransactionList'
import TransactionDetail from './pages/TransactionDetail'
import DuplicateInvestigation from './pages/DuplicateInvestigation'
import DisputeList from './pages/DisputeList'
import DisputeDetail from './pages/DisputeDetail'
import CaseList from './pages/CaseList'
import CaseDetail from './pages/CaseDetail'

export default function App() {
  return (
    <div className="flex h-screen overflow-hidden bg-gray-50">
      {/* Fixed sidebar */}
      <Sidebar />

      {/* Main scrollable content */}
      <main className="flex-1 overflow-y-auto">
        <div className="min-h-full">
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/transactions" element={<TransactionList />} />
            <Route path="/transactions/:id" element={<TransactionDetail />} />
            <Route path="/transactions/:id/duplicates/:otherId" element={<DuplicateInvestigation />} />
            <Route path="/disputes" element={<DisputeList />} />
            <Route path="/disputes/:id" element={<DisputeDetail />} />
            <Route path="/cases" element={<CaseList />} />
            <Route path="/cases/:id" element={<CaseDetail />} />
            {/* Catch-all redirect */}
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </div>
      </main>
    </div>
  )
}
