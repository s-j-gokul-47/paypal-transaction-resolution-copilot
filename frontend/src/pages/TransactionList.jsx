import React, { useEffect, useState, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { format, parseISO } from 'date-fns'
import {
  MagnifyingGlassIcon,
  PlusIcon,
  ChevronLeftIcon,
  ChevronRightIcon,
  ArrowsRightLeftIcon,
  XMarkIcon,
} from '@heroicons/react/24/outline'
import StatusBadge from '../components/StatusBadge'
import { transactionService } from '../services/api'
import toast from 'react-hot-toast'

const PAGE_SIZE = 15

const STATUS_OPTIONS = [
  { value: '', label: 'All Statuses' },
  { value: 'COMPLETED', label: 'Completed' },
  { value: 'PENDING', label: 'Pending' },
  { value: 'FAILED', label: 'Failed' },
  { value: 'REFUNDED', label: 'Refunded' },
  { value: 'DISPUTED', label: 'Disputed' },
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

function NewTransactionModal({ onClose, onCreated }) {
  const [form, setForm] = useState({
    paypal_transaction_id: '',
    merchant_id: 'MERCH-001',
    amount: '99.00',
    currency: 'USD',
    status: 'COMPLETED',
    buyer_email: '',
    description: '',
  })
  const [saving, setSaving] = useState(false)
  const [lookupId, setLookupId] = useState('TXN-MOCK-001')
  const [lookupResult, setLookupResult] = useState(null)
  const [lookupError, setLookupError] = useState(null)
  const [lookingUp, setLookingUp] = useState(false)

  async function handleSubmit(e) {
    e.preventDefault()
    if (!form.paypal_transaction_id.trim() || !form.amount) {
      toast.error('PayPal Transaction ID and Amount are required')
      return
    }
    setSaving(true)
    try {
      await transactionService.create({
        ...form,
        amount: Number(form.amount),
      })
      toast.success('Transaction recorded')
      onCreated()
      onClose()
    } catch (err) {
      toast.error(err.userMessage ?? 'Failed to record transaction')
    } finally {
      setSaving(false)
    }
  }

  async function handlePayPalLookup() {
    if (!lookupId.trim()) return
    setLookingUp(true)
    setLookupError(null)
    setLookupResult(null)
    try {
      const res = await transactionService.lookupPayPal(lookupId.trim())
      setLookupResult(res.data)
    } catch (err) {
      setLookupError(err.userMessage ?? 'Lookup failed')
    } finally {
      setLookingUp(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
      <div className="bg-white rounded-xl shadow-xl w-full max-w-lg p-6 space-y-5 max-h-[90vh] overflow-y-auto">
        <div className="flex items-center justify-between">
          <h3 className="text-base font-semibold text-gray-900">Record or Lookup Transaction</h3>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600">
            <XMarkIcon className="w-5 h-5" />
          </button>
        </div>

        {/* PayPal API Lookup preview */}
        <div className="bg-gray-50 rounded-lg p-3 border border-gray-200 space-y-2">
          <p className="text-xs font-semibold text-gray-600 uppercase tracking-wide">
            PayPal API / Mock Lookup
          </p>
          <div className="flex gap-2">
            <input
              type="text"
              className="input flex-1"
              value={lookupId}
              onChange={(e) => setLookupId(e.target.value)}
              placeholder="e.g. TXN-MOCK-001"
            />
            <button
              type="button"
              className="btn-secondary"
              onClick={handlePayPalLookup}
              disabled={lookingUp}
            >
              {lookingUp ? 'Checking…' : 'Lookup'}
            </button>
          </div>
          {lookupError && <p className="text-xs text-red-600">{lookupError}</p>}
          {lookupResult && (
            <div className="text-xs bg-white rounded border border-gray-200 p-2.5 space-y-1">
              <div className="flex items-center justify-between">
                <span className="font-semibold">{lookupResult.id} ({lookupResult.status})</span>
                <span className="px-2 py-0.5 rounded-full bg-amber-100 text-amber-800 font-semibold">
                  mode: {lookupResult.mode}
                </span>
              </div>
              <p className="text-gray-600">
                {lookupResult.amount?.value} {lookupResult.amount?.currency_code} • {lookupResult.payer?.email_address}
              </p>
              {lookupResult.warning && (
                <p className="text-amber-700">{lookupResult.warning}</p>
              )}
            </div>
          )}
        </div>

        <form onSubmit={handleSubmit} className="space-y-3">
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <div>
              <label className="block text-xs text-gray-600 mb-1">PayPal Transaction ID *</label>
              <input
                type="text"
                className="input"
                placeholder="TXN-2026-001"
                value={form.paypal_transaction_id}
                onChange={(e) => setForm({ ...form, paypal_transaction_id: e.target.value })}
              />
            </div>
            <div>
              <label className="block text-xs text-gray-600 mb-1">Merchant ID</label>
              <input
                type="text"
                className="input"
                value={form.merchant_id}
                onChange={(e) => setForm({ ...form, merchant_id: e.target.value })}
              />
            </div>
            <div>
              <label className="block text-xs text-gray-600 mb-1">Amount *</label>
              <input
                type="number"
                step="0.01"
                className="input"
                value={form.amount}
                onChange={(e) => setForm({ ...form, amount: e.target.value })}
              />
            </div>
            <div>
              <label className="block text-xs text-gray-600 mb-1">Status</label>
              <select
                className="select"
                value={form.status}
                onChange={(e) => setForm({ ...form, status: e.target.value })}
              >
                {STATUS_OPTIONS.filter((s) => s.value).map((s) => (
                  <option key={s.value} value={s.value}>{s.label}</option>
                ))}
              </select>
            </div>
          </div>
          <div>
            <label className="block text-xs text-gray-600 mb-1">Buyer Email</label>
            <input
              type="email"
              className="input"
              placeholder="buyer@example.com"
              value={form.buyer_email}
              onChange={(e) => setForm({ ...form, buyer_email: e.target.value })}
            />
          </div>
          <div>
            <label className="block text-xs text-gray-600 mb-1">Description</label>
            <input
              type="text"
              className="input"
              placeholder="Order item description"
              value={form.description}
              onChange={(e) => setForm({ ...form, description: e.target.value })}
            />
          </div>
          <div className="flex justify-end gap-2 pt-2">
            <button type="button" className="btn-secondary" onClick={onClose}>Cancel</button>
            <button type="submit" className="btn-primary" disabled={saving}>
              {saving ? 'Saving…' : 'Save Transaction'}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}

export default function TransactionList() {
  const navigate = useNavigate()

  const [transactions, setTransactions] = useState([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [showNewModal, setShowNewModal] = useState(false)

  // Filters
  const [search, setSearch] = useState('')
  const [status, setStatus] = useState('')
  const [dateFrom, setDateFrom] = useState('')
  const [dateTo, setDateTo] = useState('')

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE))

  const fetchTransactions = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const params = {
        skip: (page - 1) * PAGE_SIZE,
        limit: PAGE_SIZE,
        ...(search && { search }),
        ...(status && { status }),
        ...(dateFrom && { date_from: dateFrom }),
        ...(dateTo && { date_to: dateTo }),
      }
      const res = await transactionService.getAll(params)
      const data = res.data
      setTransactions(data?.items ?? data ?? [])
      setTotal(data?.total ?? (data?.items ?? data ?? []).length)
    } catch (err) {
      setError(err.userMessage ?? 'Failed to load transactions')
      toast.error('Failed to load transactions')
    } finally {
      setLoading(false)
    }
  }, [page, search, status, dateFrom, dateTo])

  useEffect(() => {
    fetchTransactions()
  }, [fetchTransactions])

  return (
    <div className="p-8 space-y-6">
      {showNewModal && (
        <NewTransactionModal
          onClose={() => setShowNewModal(false)}
          onCreated={fetchTransactions}
        />
      )}

      {/* Header */}
      <div className="flex items-center justify-between flex-wrap gap-4">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">Transactions</h1>
          <p className="text-sm text-gray-500 mt-0.5">
            {total.toLocaleString()} total transactions — click any row to open the resolution workspace
          </p>
        </div>
        <button className="btn-primary" onClick={() => setShowNewModal(true)}>
          <PlusIcon className="w-4 h-4" />
          New / Lookup Transaction
        </button>
      </div>

      {/* Filters */}
      <div className="bg-white rounded-xl border border-gray-200 shadow-sm p-4">
        <div className="flex flex-wrap items-end gap-3">
          <div className="relative flex-1 min-w-[200px]">
            <MagnifyingGlassIcon className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400 pointer-events-none" />
            <input
              type="text"
              placeholder="Search by PayPal ID, buyer email, or description…"
              value={search}
              onChange={(e) => { setSearch(e.target.value); setPage(1) }}
              className="input pl-9"
            />
          </div>

          <div className="min-w-[160px]">
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

          <div>
            <label className="block text-xs text-gray-500 mb-1">From</label>
            <input
              type="date"
              value={dateFrom}
              onChange={(e) => { setDateFrom(e.target.value); setPage(1) }}
              className="input"
            />
          </div>

          <div>
            <label className="block text-xs text-gray-500 mb-1">To</label>
            <input
              type="date"
              value={dateTo}
              onChange={(e) => { setDateTo(e.target.value); setPage(1) }}
              className="input"
            />
          </div>

          <button
            className="btn-secondary"
            onClick={() => { setSearch(''); setStatus(''); setDateFrom(''); setDateTo(''); setPage(1) }}
          >
            Clear
          </button>
        </div>
      </div>

      {/* Table */}
      <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
        {error ? (
          <div className="p-10 text-center">
            <p className="text-red-600 text-sm">{error}</p>
            <button className="btn-secondary mt-4" onClick={fetchTransactions}>Retry</button>
          </div>
        ) : (
          <>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="bg-gray-50 border-b border-gray-200">
                  <tr>
                    <th className="table-header">PayPal Transaction ID</th>
                    <th className="table-header">Amount</th>
                    <th className="table-header">Status</th>
                    <th className="table-header">Flags</th>
                    <th className="table-header">Buyer Email</th>
                    <th className="table-header">Date</th>
                    <th className="table-header">Actions</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100">
                  {loading
                    ? Array.from({ length: 7 }).map((_, i) => <SkeletonRow key={i} />)
                    : transactions.length === 0
                    ? (
                      <tr>
                        <td colSpan={7} className="py-16 text-center text-sm text-gray-400">
                          <ArrowsRightLeftIcon className="w-10 h-10 mx-auto mb-3 text-gray-300" />
                          No transactions found
                        </td>
                      </tr>
                    )
                    : transactions.map((txn) => (
                      <tr
                        key={txn.id}
                        className="hover:bg-gray-50 transition-colors cursor-pointer"
                        onClick={() => navigate(`/transactions/${txn.id}`)}
                      >
                        <td className="table-cell font-medium text-[#003087]">
                          {txn.paypal_transaction_id}
                        </td>
                        <td className="table-cell font-semibold">
                          {Number(txn.amount).toLocaleString('en-US', {
                            minimumFractionDigits: 2,
                            maximumFractionDigits: 2,
                          })}{' '}
                          <span className="text-xs text-gray-500 font-normal">{txn.currency ?? 'USD'}</span>
                        </td>
                        <td className="table-cell">
                          <StatusBadge status={txn.status} />
                        </td>
                        <td className="table-cell">
                          {txn.possible_duplicate_count > 0 ? (
                            <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-semibold bg-amber-100 text-amber-800 border border-amber-200">
                              Duplicate Risk ({txn.possible_duplicate_count})
                            </span>
                          ) : txn.dispute_count > 0 ? (
                            <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-semibold bg-orange-100 text-orange-800 border border-orange-200">
                              Disputed
                            </span>
                          ) : (
                            <span className="text-xs text-gray-400">—</span>
                          )}
                        </td>
                        <td className="table-cell text-gray-600">{txn.buyer_email ?? '—'}</td>
                        <td className="table-cell text-gray-500">
                          {txn.created_at ? format(parseISO(txn.created_at), 'MMM d, yyyy') : '—'}
                        </td>
                        <td className="table-cell" onClick={(e) => e.stopPropagation()}>
                          <button
                            className="text-sm text-[#003087] font-medium hover:underline"
                            onClick={() => navigate(`/transactions/${txn.id}`)}
                          >
                            Resolve
                          </button>
                        </td>
                      </tr>
                    ))}
                </tbody>
              </table>
            </div>

            {!loading && transactions.length > 0 && (
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
