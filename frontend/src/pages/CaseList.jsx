import React, { useEffect, useState, useCallback } from 'react'
import { useNavigate, Link } from 'react-router-dom'
import { format, parseISO } from 'date-fns'
import {
  FolderOpenIcon,
  ChevronLeftIcon,
  ChevronRightIcon,
} from '@heroicons/react/24/outline'
import StatusBadge from '../components/StatusBadge'
import PriorityBadge from '../components/PriorityBadge'
import { caseService } from '../services/api'
import toast from 'react-hot-toast'

const PAGE_SIZE = 15

const PRIORITY_OPTIONS = [
  { value: '', label: 'All Priorities' },
  { value: 'CRITICAL', label: 'Critical' },
  { value: 'HIGH', label: 'High' },
  { value: 'MEDIUM', label: 'Medium' },
  { value: 'LOW', label: 'Low' },
]

const OUTCOME_OPTIONS = [
  { value: '', label: 'All Outcomes' },
  { value: 'OPEN', label: 'Open' },
  { value: 'RESOLVED', label: 'Resolved' },
  { value: 'ESCALATED', label: 'Escalated' },
  { value: 'REJECTED', label: 'Rejected' },
  { value: 'REFUNDED', label: 'Refunded' },
]

function SLABadge({ breached }) {
  if (breached) {
    return (
      <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-semibold bg-red-100 text-red-700 border border-red-200">
        SLA Breached
      </span>
    )
  }
  return (
    <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-semibold bg-green-100 text-green-700 border border-green-200">
      On Track
    </span>
  )
}

function SkeletonRow() {
  return (
    <tr>
      {Array.from({ length: 7 }).map((_, i) => (
        <td key={i} className="table-cell">
          <div className="h-4 bg-gray-200 rounded animate-pulse w-full" />
        </td>
      ))}
    </tr>
  )
}

export default function CaseList() {
  const navigate = useNavigate()

  const [cases, setCases] = useState([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [priority, setPriority] = useState('')
  const [outcome, setOutcome] = useState('')

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE))

  const fetchCases = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const params = {
        skip: (page - 1) * PAGE_SIZE,
        limit: PAGE_SIZE,
        ...(priority && { priority }),
        ...(outcome && { outcome }),
      }
      const res = await caseService.getAll(params)
      const data = res.data
      setCases(data?.items ?? data ?? [])
      setTotal(data?.total ?? (data?.items ?? data ?? []).length)
    } catch (err) {
      setError(err.userMessage ?? 'Failed to load cases')
      toast.error('Failed to load cases')
    } finally {
      setLoading(false)
    }
  }, [page, priority, outcome])

  useEffect(() => {
    fetchCases()
  }, [fetchCases])

  return (
    <div className="p-8 space-y-6">
      {/* Header */}
      <div>
        <h1 className="text-2xl font-bold text-gray-900">Resolution Cases</h1>
        <p className="text-sm text-gray-500 mt-0.5">{total.toLocaleString()} total cases</p>
      </div>

      {/* Filters */}
      <div className="bg-white rounded-xl border border-gray-200 shadow-sm p-4">
        <div className="flex flex-wrap items-end gap-3">
          <div className="min-w-[160px]">
            <label className="block text-xs text-gray-500 mb-1">Priority</label>
            <select
              value={priority}
              onChange={(e) => { setPriority(e.target.value); setPage(1) }}
              className="select"
            >
              {PRIORITY_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>{o.label}</option>
              ))}
            </select>
          </div>

          <div className="min-w-[160px]">
            <label className="block text-xs text-gray-500 mb-1">Outcome / Status</label>
            <select
              value={outcome}
              onChange={(e) => { setOutcome(e.target.value); setPage(1) }}
              className="select"
            >
              {OUTCOME_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>{o.label}</option>
              ))}
            </select>
          </div>

          <button
            className="btn-secondary"
            onClick={() => { setPriority(''); setOutcome(''); setPage(1) }}
          >
            Clear Filters
          </button>
        </div>
      </div>

      {/* Table */}
      <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
        {error ? (
          <div className="p-10 text-center">
            <p className="text-red-600 text-sm">{error}</p>
            <button className="btn-secondary mt-4" onClick={fetchCases}>Retry</button>
          </div>
        ) : (
          <>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="bg-gray-50 border-b border-gray-200">
                  <tr>
                    <th className="table-header">Case ID</th>
                    <th className="table-header">Dispute ID</th>
                    <th className="table-header">Agent</th>
                    <th className="table-header">Priority</th>
                    <th className="table-header">SLA Status</th>
                    <th className="table-header">Outcome</th>
                    <th className="table-header">Actions</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100">
                  {loading
                    ? Array.from({ length: 8 }).map((_, i) => <SkeletonRow key={i} />)
                    : cases.length === 0
                    ? (
                      <tr>
                        <td colSpan={7} className="py-16 text-center text-sm text-gray-400">
                          <FolderOpenIcon className="w-10 h-10 mx-auto mb-3 text-gray-300" />
                          No resolution cases found
                        </td>
                      </tr>
                    )
                    : cases.map((c) => (
                      <tr
                        key={c.id}
                        className="hover:bg-gray-50 transition-colors cursor-pointer"
                        onClick={() => navigate(`/cases/${c.id}`)}
                      >
                        <td className="table-cell font-medium text-[#003087]">{c.id}</td>
                        <td className="table-cell">
                          {c.dispute_id ? (
                            <Link
                              to={`/disputes/${c.dispute_id}`}
                              className="text-[#009cde] hover:underline"
                              onClick={(e) => e.stopPropagation()}
                            >
                              {c.dispute_id}
                            </Link>
                          ) : '—'}
                        </td>
                        <td className="table-cell text-gray-600">{c.assigned_agent ?? '—'}</td>
                        <td className="table-cell">
                          <PriorityBadge priority={c.priority} />
                        </td>
                        <td className="table-cell">
                          <SLABadge breached={c.sla_breached} />
                        </td>
                        <td className="table-cell">
                          <StatusBadge status={c.outcome ?? c.status} />
                        </td>
                        <td className="table-cell" onClick={(e) => e.stopPropagation()}>
                          <button
                            className="text-sm text-[#003087] font-medium hover:underline"
                            onClick={() => navigate(`/cases/${c.id}`)}
                          >
                            View
                          </button>
                        </td>
                      </tr>
                    ))}
                </tbody>
              </table>
            </div>

            {/* Pagination */}
            {!loading && cases.length > 0 && (
              <div className="flex items-center justify-between px-6 py-3 border-t border-gray-100 bg-gray-50">
                <p className="text-sm text-gray-500">Page {page} of {totalPages}</p>
                <div className="flex items-center gap-1">
                  <button
                    className="btn-secondary px-2 py-1.5"
                    disabled={page === 1}
                    onClick={() => setPage((p) => Math.max(1, p - 1))}
                  >
                    <ChevronLeftIcon className="w-4 h-4" />
                  </button>
                  {Array.from({ length: Math.min(5, totalPages) }, (_, i) => {
                    const pageNum = Math.max(1, Math.min(page - 2, totalPages - 4)) + i
                    return (
                      <button
                        key={pageNum}
                        className={`px-3 py-1 text-sm rounded-md font-medium transition-colors ${
                          pageNum === page
                            ? 'bg-[#003087] text-white'
                            : 'text-gray-600 hover:bg-gray-200'
                        }`}
                        onClick={() => setPage(pageNum)}
                      >
                        {pageNum}
                      </button>
                    )
                  })}
                  <button
                    className="btn-secondary px-2 py-1.5"
                    disabled={page === totalPages}
                    onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
                  >
                    <ChevronRightIcon className="w-4 h-4" />
                  </button>
                </div>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  )
}
