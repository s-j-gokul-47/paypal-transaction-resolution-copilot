import React, { useEffect, useState, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { format, parseISO } from 'date-fns'
import {
  ExclamationTriangleIcon,
  ChevronLeftIcon,
  ChevronRightIcon,
} from '@heroicons/react/24/outline'
import StatusBadge from '../components/StatusBadge'
import PriorityBadge from '../components/PriorityBadge'
import { disputeService } from '../services/api'
import toast from 'react-hot-toast'

const PAGE_SIZE = 15

const STATUS_OPTIONS = [
  { value: '', label: 'All Statuses' },
  { value: 'OPEN', label: 'Open' },
  { value: 'UNDER_REVIEW', label: 'Under Review' },
  { value: 'RESOLVED', label: 'Resolved' },
  { value: 'CLOSED', label: 'Closed' },
]

const REASON_OPTIONS = [
  { value: '', label: 'All Reasons' },
  { value: 'UNAUTHORIZED', label: 'Unauthorized Transaction' },
  { value: 'MERCHANDISE_OR_SERVICE_NOT_RECEIVED', label: 'Merchandise / Service Not Received' },
  { value: 'SIGNIFICANTLY_NOT_AS_DESCRIBED', label: 'Significantly Not As Described' },
  { value: 'DUPLICATE_TRANSACTION', label: 'Duplicate Transaction' },
  { value: 'INCORRECT_AMOUNT', label: 'Incorrect Amount' },
]

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

export default function DisputeList() {
  const navigate = useNavigate()

  const [disputes, setDisputes] = useState([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [status, setStatus] = useState('')
  const [reason, setReason] = useState('')

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE))

  const fetchDisputes = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const params = {
        skip: (page - 1) * PAGE_SIZE,
        limit: PAGE_SIZE,
        ...(status && { status }),
        ...(reason && { reason }),
      }
      const res = await disputeService.getAll(params)
      const data = res.data
      setDisputes(data?.items ?? data ?? [])
      setTotal(data?.total ?? (data?.items ?? data ?? []).length)
    } catch (err) {
      setError(err.userMessage ?? 'Failed to load disputes')
      toast.error('Failed to load disputes')
    } finally {
      setLoading(false)
    }
  }, [page, status, reason])

  useEffect(() => {
    fetchDisputes()
  }, [fetchDisputes])

  return (
    <div className="p-8 space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">Disputes</h1>
          <p className="text-sm text-gray-500 mt-0.5">{total.toLocaleString()} total disputes</p>
        </div>
      </div>

      {/* Filters */}
      <div className="bg-white rounded-xl border border-gray-200 shadow-sm p-4">
        <div className="flex flex-wrap items-end gap-3">
          <div className="min-w-[180px]">
            <label className="block text-xs text-gray-500 mb-1">Status</label>
            <select
              value={status}
              onChange={(e) => { setStatus(e.target.value); setPage(1) }}
              className="select"
            >
              {STATUS_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>{o.label}</option>
              ))}
            </select>
          </div>

          <div className="min-w-[200px]">
            <label className="block text-xs text-gray-500 mb-1">Reason</label>
            <select
              value={reason}
              onChange={(e) => { setReason(e.target.value); setPage(1) }}
              className="select"
            >
              {REASON_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>{o.label}</option>
              ))}
            </select>
          </div>

          <button
            className="btn-secondary"
            onClick={() => { setStatus(''); setReason(''); setPage(1) }}
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
            <button className="btn-secondary mt-4" onClick={fetchDisputes}>Retry</button>
          </div>
        ) : (
          <>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="bg-gray-50 border-b border-gray-200">
                  <tr>
                    <th className="table-header">Dispute ID</th>
                    <th className="table-header">Reason</th>
                    <th className="table-header">Status</th>
                    <th className="table-header">Priority</th>
                    <th className="table-header">Transaction</th>
                    <th className="table-header">Created</th>
                    <th className="table-header">Actions</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100">
                  {loading
                    ? Array.from({ length: 8 }).map((_, i) => <SkeletonRow key={i} />)
                    : disputes.length === 0
                    ? (
                      <tr>
                        <td colSpan={7} className="py-16 text-center text-sm text-gray-400">
                          <ExclamationTriangleIcon className="w-10 h-10 mx-auto mb-3 text-gray-300" />
                          No disputes found
                        </td>
                      </tr>
                    )
                    : disputes.map((d) => (
                      <tr
                        key={d.id}
                        className="hover:bg-gray-50 transition-colors cursor-pointer"
                        onClick={() => navigate(`/disputes/${d.id}`)}
                      >
                        <td className="table-cell font-medium text-[#003087]">{d.id}</td>
                        <td className="table-cell text-gray-600">
                          {d.reason?.replace(/_/g, ' ') ?? '—'}
                        </td>
                        <td className="table-cell">
                          <StatusBadge status={d.status} />
                        </td>
                        <td className="table-cell">
                          <PriorityBadge priority={d.priority} />
                        </td>
                        <td className="table-cell text-gray-500">{d.transaction_id ?? '—'}</td>
                        <td className="table-cell text-gray-500">
                          {d.created_at ? format(parseISO(d.created_at), 'MMM d, yyyy') : '—'}
                        </td>
                        <td className="table-cell" onClick={(e) => e.stopPropagation()}>
                          <button
                            className="text-sm text-[#003087] font-medium hover:underline"
                            onClick={() => navigate(`/disputes/${d.id}`)}
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
            {!loading && disputes.length > 0 && (
              <div className="flex items-center justify-between px-6 py-3 border-t border-gray-100 bg-gray-50">
                <p className="text-sm text-gray-500">
                  Page {page} of {totalPages}
                </p>
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
