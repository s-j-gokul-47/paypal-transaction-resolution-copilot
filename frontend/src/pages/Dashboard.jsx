import React, { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  PieChart,
  Pie,
  Cell,
  Tooltip,
  Legend,
  ResponsiveContainer,
} from 'recharts'
import {
  ArrowsRightLeftIcon,
  ExclamationTriangleIcon,
  DocumentDuplicateIcon,
  ClockIcon,
} from '@heroicons/react/24/outline'
import { format, parseISO } from 'date-fns'
import StatsCard from '../components/StatsCard'
import StatusBadge from '../components/StatusBadge'
import { analyticsService, transactionService } from '../services/api'

const PIE_COLORS = {
  COMPLETED: '#34d399',
  PENDING: '#fbbf24',
  FAILED: '#f87171',
  DISPUTED: '#fb923c',
  REFUNDED: '#60a5fa',
}
const DEFAULT_PIE_COLOR = '#d1d5db'

export default function Dashboard() {
  const [summary, setSummary] = useState(null)
  const [transactions, setTransactions] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  useEffect(() => {
    async function fetchData() {
      try {
        setLoading(true)
        setError(null)
        const [summaryRes, txnRes] = await Promise.all([
          analyticsService.getSummary(),
          transactionService.getAll({ limit: 8 }),
        ])
        setSummary(summaryRes.data)
        setTransactions(txnRes.data?.items ?? txnRes.data ?? [])
      } catch (err) {
        setError(err.userMessage ?? 'Failed to load dashboard data from backend')
      } finally {
        setLoading(false)
      }
    }
    fetchData()
  }, [])

  const pieData = summary?.transaction_status_breakdown ?? []
  const duplicateAlerts = summary?.duplicate_alerts ?? []
  const recentActivity = summary?.recent_activity ?? []

  return (
    <div className="p-8 space-y-6">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">Transaction Resolution Overview</h1>
          <p className="text-sm text-gray-500 mt-1">
            Inspect payment states, timelines, duplicate alerts, and support summaries from verified records
          </p>
        </div>
        <Link to="/transactions" className="btn-primary">
          <ArrowsRightLeftIcon className="w-4 h-4" />
          Open Transactions
        </Link>
      </div>

      {/* Environment banner */}
      {summary?.services?.paypal === 'mock' && (
        <div className="bg-amber-50 border border-amber-200 rounded-xl px-4 py-3 text-sm text-amber-900 flex items-center justify-between gap-4">
          <span>
            <strong>Mock / Local Development Mode:</strong> PayPal API actions return explicitly tagged simulated responses and AI explanations run via deterministic record-grounded heuristics.
          </span>
          <span className="text-xs font-semibold uppercase tracking-wide px-2.5 py-1 rounded-full bg-amber-100 border border-amber-300 flex-shrink-0">
            Mock Mode
          </span>
        </div>
      )}

      {error && (
        <div className="bg-red-50 border border-red-200 rounded-xl p-4 text-sm text-red-700">
          {error}
        </div>
      )}

      {/* Stats Row */}
      <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-4">
        <StatsCard
          title="Total Transactions"
          value={summary?.total_transactions ?? 0}
          icon={<ArrowsRightLeftIcon className="w-5 h-5" />}
          loading={loading}
        />
        <StatsCard
          title="Needs Attention"
          value={summary?.attention_needed_count ?? 0}
          icon={<ClockIcon className="w-5 h-5" />}
          loading={loading}
        />
        <StatsCard
          title="Duplicate Alerts"
          value={summary?.duplicate_alerts_count ?? 0}
          icon={<DocumentDuplicateIcon className="w-5 h-5" />}
          loading={loading}
        />
        <StatsCard
          title="Open Disputes (Secondary)"
          value={summary?.open_disputes ?? 0}
          icon={<ExclamationTriangleIcon className="w-5 h-5" />}
          loading={loading}
        />
      </div>

      {/* Duplicate Alerts Banner/Card */}
      {!loading && duplicateAlerts.length > 0 && (
        <div className="bg-white rounded-xl border border-amber-200 shadow-sm p-5 space-y-3">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2">
              <DocumentDuplicateIcon className="w-5 h-5 text-amber-600" />
              <h2 className="text-base font-semibold text-gray-900">
                Possible Duplicate Payments Detected ({duplicateAlerts.length})
              </h2>
            </div>
            <span className="text-xs text-gray-500">Matched on buyer email, merchant, amount & time window</span>
          </div>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            {duplicateAlerts.map((alert, idx) => (
              <div
                key={`${alert.primary_id}-${alert.duplicate_id}-${idx}`}
                className="rounded-lg border border-amber-200 bg-amber-50/50 p-3 flex items-center justify-between gap-3"
              >
                <div className="text-sm space-y-1">
                  <div className="flex items-center gap-2 flex-wrap">
                    <p className="font-semibold text-gray-900">
                      {alert.primary_paypal_id} &amp; {alert.duplicate_paypal_id}
                    </p>
                    {alert.review_decision && (
                      <span
                        className={`px-2 py-0.5 rounded-full text-[11px] font-semibold border ${
                          alert.review_decision === 'CONFIRMED_DUPLICATE'
                            ? 'bg-red-100 text-red-800 border-red-200'
                            : 'bg-amber-100 text-amber-800 border-amber-200'
                        }`}
                      >
                        {alert.review_decision.replace(/_/g, ' ')}
                      </span>
                    )}
                  </div>
                  <p className="text-gray-600 text-xs">
                    {alert.buyer_email} • {Number(alert.amount).toFixed(2)} {alert.currency} •{' '}
                    {Math.max(1, Math.round(alert.time_diff_seconds / 60))} min apart ({alert.confidence} confidence)
                  </p>
                </div>
                <div className="flex flex-col items-end gap-1 flex-shrink-0">
                  <Link
                    to={`/transactions/${alert.primary_id}/duplicates/${alert.duplicate_id}`}
                    className="text-xs font-semibold text-[#003087] hover:underline"
                  >
                    Investigate Pair →
                  </Link>
                  <Link
                    to={`/transactions/${alert.duplicate_id}`}
                    className="text-[11px] text-gray-500 hover:text-gray-800 hover:underline"
                  >
                    Inspect Txn
                  </Link>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Main grid */}
      <div className="grid grid-cols-1 xl:grid-cols-3 gap-6">
        {/* Recent Transactions Table */}
        <div className="xl:col-span-2 bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
          <div className="px-6 py-4 border-b border-gray-100 flex items-center justify-between">
            <h2 className="text-base font-semibold text-gray-900">Recent Transactions</h2>
            <Link to="/transactions" className="text-sm text-[#003087] font-medium hover:underline">
              View all →
            </Link>
          </div>

          {loading ? (
            <div className="p-6 space-y-3">
              {Array.from({ length: 5 }).map((_, i) => (
                <div key={i} className="h-8 bg-gray-100 rounded animate-pulse" />
              ))}
            </div>
          ) : transactions.length === 0 ? (
            <div className="p-10 text-center text-sm text-gray-400">
              No transactions recorded yet.
            </div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="bg-gray-50">
                  <tr>
                    <th className="table-header">PayPal ID</th>
                    <th className="table-header">Amount</th>
                    <th className="table-header">Status</th>
                    <th className="table-header">Flags</th>
                    <th className="table-header">Buyer</th>
                    <th className="table-header">Created</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100">
                  {transactions.map((txn) => (
                    <tr key={txn.id} className="hover:bg-gray-50 transition-colors">
                      <td className="table-cell">
                        <Link
                          to={`/transactions/${txn.id}`}
                          className="text-[#003087] font-medium hover:underline"
                        >
                          {txn.paypal_transaction_id}
                        </Link>
                      </td>
                      <td className="table-cell font-semibold">
                        {Number(txn.amount).toFixed(2)} {txn.currency}
                      </td>
                      <td className="table-cell">
                        <StatusBadge status={txn.status} />
                      </td>
                      <td className="table-cell">
                        {txn.possible_duplicate_count > 0 ? (
                          <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-semibold bg-amber-100 text-amber-800 border border-amber-200">
                            Possible Duplicate
                          </span>
                        ) : txn.dispute_count > 0 ? (
                          <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-semibold bg-orange-100 text-orange-800 border border-orange-200">
                            {txn.dispute_count} Dispute
                          </span>
                        ) : (
                          <span className="text-xs text-gray-400">—</span>
                        )}
                      </td>
                      <td className="table-cell text-gray-600">{txn.buyer_email ?? '—'}</td>
                      <td className="table-cell text-gray-500">
                        {txn.created_at ? format(parseISO(txn.created_at), 'MMM d, yyyy') : '—'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>

        {/* Right column */}
        <div className="space-y-6">
          <div className="bg-white rounded-xl border border-gray-200 shadow-sm p-6">
            <h2 className="text-base font-semibold text-gray-900 mb-4">Transaction Status Breakdown</h2>
            {pieData.length === 0 ? (
              <p className="text-sm text-gray-400 text-center py-6">No data</p>
            ) : (
              <ResponsiveContainer width="100%" height={220}>
                <PieChart>
                  <Pie
                    data={pieData}
                    cx="50%"
                    cy="50%"
                    innerRadius={55}
                    outerRadius={80}
                    paddingAngle={3}
                    dataKey="value"
                  >
                    {pieData.map((entry, index) => (
                      <Cell
                        key={`cell-${index}`}
                        fill={PIE_COLORS[entry.name] ?? DEFAULT_PIE_COLOR}
                      />
                    ))}
                  </Pie>
                  <Tooltip formatter={(value, name) => [value, name]} />
                  <Legend
                    iconType="circle"
                    iconSize={8}
                    formatter={(value) => (
                      <span className="text-xs text-gray-600">{value}</span>
                    )}
                  />
                </PieChart>
              </ResponsiveContainer>
            )}
          </div>

          <div className="bg-white rounded-xl border border-gray-200 shadow-sm p-6">
            <h2 className="text-base font-semibold text-gray-900 mb-4">Recent Payment Events</h2>
            {recentActivity.length === 0 ? (
              <p className="text-sm text-gray-400">No events recorded yet.</p>
            ) : (
              <ul className="space-y-3">
                {recentActivity.map((item) => (
                  <li key={item.id} className="flex items-start gap-3">
                    <span className="mt-1.5 w-2 h-2 rounded-full bg-[#009cde] flex-shrink-0" />
                    <div className="min-w-0">
                      <Link
                        to={`/transactions/${item.transaction_id}`}
                        className="text-sm font-medium text-gray-800 hover:text-[#003087] hover:underline block"
                      >
                        {item.title}
                      </Link>
                      <p className="text-xs text-gray-500 line-clamp-2">{item.description}</p>
                      <p className="text-[11px] text-gray-400 mt-0.5">
                        {item.occurred_at ? format(parseISO(item.occurred_at), 'MMM d, HH:mm') : ''} • {item.source}
                      </p>
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
