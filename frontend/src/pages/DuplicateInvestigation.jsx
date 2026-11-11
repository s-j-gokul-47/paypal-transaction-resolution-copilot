import React, { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { format, parseISO } from 'date-fns'
import {
  ArrowLeftIcon,
  CheckCircleIcon,
  ClockIcon,
  DocumentDuplicateIcon,
  ExclamationTriangleIcon,
  InformationCircleIcon,
  ShieldExclamationIcon,
  XCircleIcon,
} from '@heroicons/react/24/outline'
import toast from 'react-hot-toast'
import StatusBadge from '../components/StatusBadge'
import { transactionService } from '../services/api'

const DECISION_LABELS = {
  PENDING_REVIEW: {
    label: 'Pending Review',
    className: 'bg-amber-100 text-amber-800 border-amber-300',
  },
  CONFIRMED_DUPLICATE: {
    label: 'Confirmed Duplicate',
    className: 'bg-red-100 text-red-800 border-red-300',
  },
  SEPARATE_ORDERS: {
    label: 'Separate Orders',
    className: 'bg-emerald-100 text-emerald-800 border-emerald-300',
  },
}

const MATCH_STATUS_STYLES = {
  MATCH: 'bg-emerald-100 text-emerald-800 border-emerald-200',
  VARIANT: 'bg-amber-100 text-amber-800 border-amber-200',
  DIFFERENT: 'bg-rose-100 text-rose-800 border-rose-200',
  UNAVAILABLE: 'bg-gray-100 text-gray-600 border-gray-200',
}

function DecisionPill({ decision }) {
  const cfg = DECISION_LABELS[decision] ?? {
    label: decision || 'Unknown',
    className: 'bg-gray-100 text-gray-700 border-gray-200',
  }
  return (
    <span
      className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-semibold border ${cfg.className}`}
    >
      {cfg.label}
    </span>
  )
}

function ReviewerIdentityBadge({ verified }) {
  return verified ? (
    <span className="inline-flex items-center px-2 py-0.5 rounded-full text-[11px] font-medium bg-emerald-100 text-emerald-800 border border-emerald-200">
      Verified reviewer
    </span>
  ) : (
    <span className="inline-flex items-center px-2 py-0.5 rounded-full text-[11px] font-medium bg-gray-100 text-gray-600 border border-gray-200">
      Unverified identifier (no auth)
    </span>
  )
}

function formatTimestamp(ts) {
  if (!ts) return 'Unavailable'
  try {
    return format(parseISO(ts), 'MMM d, yyyy HH:mm:ss')
  } catch {
    return ts
  }
}

function formatDuration(seconds) {
  if (seconds === null || seconds === undefined) return 'Unavailable'
  const total = Number(seconds)
  if (Number.isNaN(total)) return 'Unavailable'
  if (total < 60) return `${total} sec`
  const mins = Math.floor(total / 60)
  const secs = total % 60
  if (mins < 60) return secs > 0 ? `${mins} min ${secs} sec` : `${mins} min`
  const hrs = Math.floor(mins / 60)
  const remMins = mins % 60
  return remMins > 0 ? `${hrs} hr ${remMins} min` : `${hrs} hr`
}

function TransactionSummaryCard({ txn, accentClass }) {
  if (!txn) return null
  const isOriginal = txn.role === 'ORIGINAL'

  const fields = [
    { label: 'Order ID', value: txn.order_id || 'Unavailable' },
    { label: 'Description', value: txn.description || 'Unavailable' },
    { label: 'Buyer Email', value: txn.buyer_email || 'Unavailable' },
    { label: 'Merchant ID', value: txn.merchant_id || 'Unavailable' },
    { label: 'Timestamp', value: formatTimestamp(txn.created_at) },
    { label: 'Internal ID', value: txn.id },
    {
      label: 'Refunded / Remaining',
      value: `${Number(txn.total_refunded ?? 0).toFixed(2)} refunded • ${Number(
        txn.remaining_refundable ?? 0
      ).toFixed(2)} ${txn.currency ?? 'USD'} remaining`,
    },
    {
      label: 'Manual Refund Eligible',
      value: txn.refund_eligible ? 'Yes (manual only)' : 'No',
    },
  ]

  return (
    <div className={`card border-t-4 ${accentClass} space-y-4`}>
      <div className="flex items-start justify-between gap-3 flex-wrap">
        <div className="space-y-1">
          <div className="flex items-center gap-2 flex-wrap">
            <span
              className={`px-2.5 py-0.5 rounded-full text-xs font-bold border ${
                isOriginal
                  ? 'bg-blue-50 text-[#003087] border-blue-200'
                  : 'bg-amber-50 text-amber-800 border-amber-300'
              }`}
            >
              {txn.role}
            </span>
            <StatusBadge status={txn.status} />
          </div>
          <h2 className="text-lg font-bold text-gray-900">
            <Link
              to={`/transactions/${txn.id}`}
              className="text-[#003087] hover:underline"
            >
              {txn.paypal_transaction_id || txn.id}
            </Link>
          </h2>
        </div>
        <div className="text-right">
          <p className="text-xl font-bold text-gray-900">
            {Number(txn.amount ?? 0).toFixed(2)} {txn.currency ?? 'USD'}
          </p>
          <p className="text-xs text-gray-500">
            {isOriginal ? 'Earlier recorded charge' : 'Later candidate charge'}
          </p>
        </div>
      </div>

      <dl className="grid grid-cols-1 sm:grid-cols-2 gap-x-4 gap-y-3 pt-3 border-t border-gray-100">
        {fields.map((item) => (
          <div key={item.label}>
            <dt className="text-[11px] font-semibold uppercase tracking-wide text-gray-500">
              {item.label}
            </dt>
            <dd className="mt-0.5 text-sm text-gray-900 break-words">{item.value}</dd>
          </div>
        ))}
      </dl>
    </div>
  )
}

export default function DuplicateInvestigation() {
  const { id, otherId } = useParams()
  const navigate = useNavigate()

  const [investigation, setInvestigation] = useState(null)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState(null)

  // Review form state
  const [selectedDecision, setSelectedDecision] = useState('')
  const [reviewer, setReviewer] = useState('')
  const [notes, setNotes] = useState('')
  const [overrideReason, setOverrideReason] = useState('')
  const [formValidationError, setFormValidationError] = useState(null)
  const [submitBanner, setSubmitBanner] = useState(null)
  const [submitting, setSubmitting] = useState(false)

  const isMountedRef = useRef(true)
  const activePairKeyRef = useRef('')
  const fetchSeqRef = useRef(0)

  useEffect(() => {
    isMountedRef.current = true
    return () => {
      isMountedRef.current = false
      fetchSeqRef.current += 1
    }
  }, [])

  const fetchInvestigation = useCallback(
    async ({ silent = false } = {}) => {
      const requestPairKey = `${id ?? ''}::${otherId ?? ''}`
      activePairKeyRef.current = requestPairKey
      const requestId = ++fetchSeqRef.current
      const isStale = () =>
        !isMountedRef.current ||
        activePairKeyRef.current !== requestPairKey ||
        fetchSeqRef.current !== requestId

      if (!silent) {
        setLoading(true)
      }
      setLoadError(null)
      try {
        const res = await transactionService.getDuplicateInvestigation(id, otherId)
        if (isStale()) return null
        setInvestigation(res.data)
        return res.data
      } catch (err) {
        if (isStale()) return null
        setLoadError(err.userMessage ?? 'Failed to load duplicate investigation')
        return null
      } finally {
        if (!silent && !isStale()) {
          setLoading(false)
        }
      }
    },
    [id, otherId]
  )

  useEffect(() => {
    activePairKeyRef.current = `${id ?? ''}::${otherId ?? ''}`
    setInvestigation(null)
    setSelectedDecision('')
    setReviewer('')
    setNotes('')
    setOverrideReason('')
    setFormValidationError(null)
    setSubmitBanner(null)
    setSubmitting(false)
    fetchInvestigation()

    return () => {
      fetchSeqRef.current += 1
    }
  }, [id, otherId, fetchInvestigation])

  const review = investigation?.review
  const currentDecision = review?.decision ?? 'PENDING_REVIEW'
  const hasFinalDecision =
    currentDecision === 'CONFIRMED_DUPLICATE' || currentDecision === 'SEPARATE_ORDERS'
  const isReversal =
    hasFinalDecision && Boolean(selectedDecision) && selectedDecision !== currentDecision
  const isSameAsCurrent = hasFinalDecision && selectedDecision === currentDecision

  async function handleSubmitReview(e) {
    e.preventDefault()
    setFormValidationError(null)
    setSubmitBanner(null)

    if (!selectedDecision) {
      setFormValidationError('Please select CONFIRMED_DUPLICATE or SEPARATE_ORDERS.')
      return
    }
    if (isSameAsCurrent) {
      setFormValidationError(
        `This pair is already marked ${currentDecision}. Choose the other decision to override it.`
      )
      return
    }
    if (isReversal && !overrideReason.trim()) {
      setFormValidationError(
        'An override reason is required when changing an existing final review decision.'
      )
      return
    }

    const submitPairKey = `${id ?? ''}::${otherId ?? ''}`
    const isSubmitStale = () =>
      !isMountedRef.current || activePairKeyRef.current !== submitPairKey

    setSubmitting(true)
    try {
      const payload = {
        decision: selectedDecision,
        reviewer: reviewer.trim() || undefined,
        notes: notes.trim() || undefined,
        override_reason: isReversal ? overrideReason.trim() : undefined,
      }
      const res = await transactionService.submitDuplicateReview(id, otherId, payload)
      if (isSubmitStale()) return
      toast.success(`Review saved: ${res.data.decision.replace(/_/g, ' ')}`)
      setOverrideReason('')
      setNotes('')
      setSelectedDecision('')
      setSubmitBanner({
        type: 'success',
        message: `Decision recorded as ${res.data.decision}. No refund was triggered.`,
      })
      await fetchInvestigation({ silent: true })
    } catch (err) {
      if (isSubmitStale()) return
      const status = err.response?.status
      const backendMsg = err.userMessage ?? 'Failed to submit review decision'

      if (status === 409) {
        toast.error('Review state changed or decision already recorded. Refreshing…')
        await fetchInvestigation({ silent: true })
        if (isSubmitStale()) return
        setSubmitBanner({
          type: 'conflict',
          message: `409 Conflict: ${backendMsg}. The review state has changed or the same decision was already submitted — the investigation view above has been refreshed.`,
        })
      } else if (status === 422) {
        toast.error(backendMsg)
        setSubmitBanner({
          type: 'validation',
          message: `422 Validation Error: ${backendMsg}`,
        })
      } else {
        toast.error(backendMsg)
        setSubmitBanner({
          type: 'network',
          message: `Submission failed (${backendMsg}). Your entered form values have been preserved so you can retry.`,
        })
      }
    } finally {
      if (!isSubmitStale()) {
        setSubmitting(false)
      }
    }
  }

  if (loading) {
    return (
      <div className="p-8 space-y-6">
        <div className="h-8 bg-gray-200 rounded w-64 animate-pulse" />
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          <div className="card h-56 animate-pulse bg-gray-100" />
          <div className="card h-56 animate-pulse bg-gray-100" />
        </div>
      </div>
    )
  }

  if (loadError) {
    return (
      <div className="p-8 space-y-4">
        <button className="btn-secondary" onClick={() => navigate(-1)}>
          <ArrowLeftIcon className="w-4 h-4" /> Back
        </button>
        <div className="card border-red-200 bg-red-50 text-red-800 space-y-2">
          <p className="font-semibold">Unable to load duplicate investigation</p>
          <p className="text-sm">{loadError}</p>
        </div>
      </div>
    )
  }

  const orig = investigation?.original_transaction
  const extra = investigation?.extra_candidate
  const comparison = investigation?.comparison
  const history = [...(review?.decision_history ?? [])].reverse()

  return (
    <div className="p-8 space-y-6">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div className="flex items-center gap-4 flex-wrap">
          <Link
            to={`/transactions/${id}`}
            className="btn-secondary"
          >
            <ArrowLeftIcon className="w-4 h-4" />
            Back to Transaction
          </Link>
          <div>
            <div className="flex items-center gap-3 flex-wrap">
              <DocumentDuplicateIcon className="w-5 h-5 text-[#003087]" />
              <h1 className="text-xl font-bold text-gray-900">
                Duplicate Pair Investigation
              </h1>
              <DecisionPill decision={currentDecision} />
            </div>
            <p className="text-xs text-gray-500 mt-1">
              Canonical Pair: {investigation?.pair_low_id} ↔ {investigation?.pair_high_id}
            </p>
          </div>
        </div>
        <button
          type="button"
          className="btn-secondary"
          onClick={() => fetchInvestigation()}
        >
          Refresh Investigation
        </button>
      </div>

      {/* Refund Separation Guardrail Banner */}
      <div className="bg-blue-50 border border-blue-200 rounded-xl px-4 py-3 text-sm text-blue-950 flex items-start gap-3">
        <InformationCircleIcon className="w-5 h-5 text-[#003087] flex-shrink-0 mt-0.5" />
        <div>
          <strong>Human Review Only — No Automatic Refunds:</strong> This workspace helps you verify
          whether two charges belong to one duplicated order or separate orders. Recording a review
          decision logs audit events on both transactions and never initiates a refund.
        </div>
      </div>

      {/* Side-by-Side Comparison (using backend-provided ORIGINAL and EXTRA_CANDIDATE roles) */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <TransactionSummaryCard txn={orig} accentClass="border-t-[#003087]" />
        <TransactionSummaryCard txn={extra} accentClass="border-t-amber-500" />
      </div>

      {/* Comparison Matrix & Evidence */}
      <div className="card space-y-5">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h2 className="text-base font-semibold text-gray-900">
              Side-by-Side Field Comparison
            </h2>
            <p className="text-xs text-gray-500 mt-0.5">
              Time between charges: <strong>{formatDuration(comparison?.time_diff_seconds)}</strong>
            </p>
          </div>
          <div className="flex items-center gap-2 flex-wrap">
            {comparison?.classification && (
              <span className="px-2.5 py-0.5 rounded-full text-xs font-semibold bg-amber-100 text-amber-900 border border-amber-200">
                {comparison.classification.replace(/_/g, ' ')}
              </span>
            )}
            {comparison?.confidence && (
              <span className="px-2.5 py-0.5 rounded-full text-xs font-semibold bg-gray-100 text-gray-800 border border-gray-300">
                {comparison.confidence} Confidence
              </span>
            )}
            {comparison?.order_relationship && (
              <span className="px-2.5 py-0.5 rounded-full text-xs font-semibold bg-blue-50 text-[#003087] border border-blue-200">
                Order: {comparison.order_relationship.replace(/_/g, ' ')}
              </span>
            )}
          </div>
        </div>

        {comparison?.explanation && (
          <div className="bg-gray-50 border border-gray-200 rounded-lg p-4 space-y-1.5">
            <p className="text-sm text-gray-800">{comparison.explanation}</p>
            {comparison?.recommended_action && (
              <p className="text-xs text-gray-600">
                <strong>Recommended Next Step:</strong> {comparison.recommended_action}
              </p>
            )}
          </div>
        )}

        <div className="overflow-x-auto border border-gray-200 rounded-lg">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 border-b border-gray-200">
              <tr>
                <th className="table-header">Field</th>
                <th className="table-header">Original ({orig?.paypal_transaction_id})</th>
                <th className="table-header">Extra Candidate ({extra?.paypal_transaction_id})</th>
                <th className="table-header">Comparison</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {(comparison?.field_matrix ?? []).map((row) => (
                <tr key={row.field} className="hover:bg-gray-50">
                  <td className="table-cell font-semibold text-gray-900 whitespace-nowrap">
                    {row.field}
                  </td>
                  <td className="table-cell text-gray-700 break-words">
                    {row.original_value ?? 'Unavailable'}
                  </td>
                  <td className="table-cell text-gray-700 break-words">
                    {row.extra_value ?? 'Unavailable'}
                  </td>
                  <td className="table-cell whitespace-nowrap">
                    <span
                      className={`inline-flex items-center px-2 py-0.5 rounded-full text-[11px] font-semibold border ${
                        MATCH_STATUS_STYLES[row.match_status] ??
                        'bg-gray-100 text-gray-700 border-gray-200'
                      }`}
                    >
                      {row.match_status}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div className="bg-emerald-50/50 border border-emerald-200 rounded-lg p-4 space-y-2">
            <h3 className="text-xs font-semibold uppercase tracking-wide text-emerald-900">
              Matching Fields ({comparison?.matching_fields?.length ?? 0})
            </h3>
            {comparison?.matching_fields?.length ? (
              <div className="flex flex-wrap gap-1.5">
                {comparison.matching_fields.map((field) => (
                  <span
                    key={field}
                    className="px-2 py-0.5 rounded bg-white border border-emerald-200 text-xs font-medium text-emerald-800"
                  >
                    {field}
                  </span>
                ))}
              </div>
            ) : (
              <p className="text-xs text-gray-500">No matching fields reported.</p>
            )}
          </div>

          <div className="bg-rose-50/50 border border-rose-200 rounded-lg p-4 space-y-2">
            <h3 className="text-xs font-semibold uppercase tracking-wide text-rose-900">
              Key Differences ({comparison?.differences?.length ?? 0})
            </h3>
            {comparison?.differences?.length ? (
              <ul className="list-disc list-inside text-xs text-rose-900 space-y-1">
                {comparison.differences.map((diff, idx) => (
                  <li key={idx} className="break-words">
                    {diff}
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-xs text-gray-500">No field differences reported.</p>
            )}
          </div>
        </div>
      </div>

      {/* Review Decision Form + Decision History */}
      <div className="grid grid-cols-1 xl:grid-cols-2 gap-6">
        {/* Submit / Override Decision */}
        <div className="card space-y-4">
          <div className="flex items-center justify-between gap-2 flex-wrap">
            <h2 className="text-base font-semibold text-gray-900">
              {hasFinalDecision ? 'Update or Override Review Decision' : 'Submit Review Decision'}
            </h2>
            <DecisionPill decision={currentDecision} />
          </div>

          {review?.id && (
            <div className="rounded-lg bg-gray-50 border border-gray-200 p-3 text-xs text-gray-700 space-y-1">
              <div className="flex items-center gap-2 flex-wrap">
                <span>
                  <strong>Recorded Reviewer:</strong> {review.reviewer || 'unauthenticated-agent'}
                </span>
                <ReviewerIdentityBadge verified={review.reviewer_verified} />
              </div>
              {review.notes && (
                <p>
                  <strong>Latest Notes:</strong> {review.notes}
                </p>
              )}
              <p className="text-gray-500">
                Last updated: {formatTimestamp(review.updated_at)}
              </p>
            </div>
          )}

          {submitBanner && (
            <div
              role="alert"
              className={`rounded-lg border p-3 text-sm flex items-start gap-2.5 ${
                submitBanner.type === 'success'
                  ? 'bg-emerald-50 border-emerald-200 text-emerald-900'
                  : submitBanner.type === 'conflict'
                  ? 'bg-amber-50 border-amber-300 text-amber-950'
                  : 'bg-red-50 border-red-200 text-red-900'
              }`}
            >
              {submitBanner.type === 'success' ? (
                <CheckCircleIcon className="w-5 h-5 text-emerald-600 flex-shrink-0 mt-0.5" />
              ) : submitBanner.type === 'conflict' ? (
                <ExclamationTriangleIcon className="w-5 h-5 text-amber-600 flex-shrink-0 mt-0.5" />
              ) : (
                <XCircleIcon className="w-5 h-5 text-red-600 flex-shrink-0 mt-0.5" />
              )}
              <span>{submitBanner.message}</span>
            </div>
          )}

          {formValidationError && (
            <div
              role="alert"
              className="rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-800"
            >
              {formValidationError}
            </div>
          )}

          <form onSubmit={handleSubmitReview} className="space-y-4">
            <div className="space-y-2">
              <label className="block text-xs font-semibold uppercase tracking-wide text-gray-600">
                Decision
              </label>
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                <button
                  type="button"
                  onClick={() => {
                    setSelectedDecision('CONFIRMED_DUPLICATE')
                    setFormValidationError(null)
                  }}
                  className={`text-left p-3.5 rounded-lg border-2 transition-colors ${
                    selectedDecision === 'CONFIRMED_DUPLICATE'
                      ? 'border-red-500 bg-red-50'
                      : 'border-gray-200 hover:border-gray-300 bg-white'
                  }`}
                >
                  <p className="text-sm font-semibold text-gray-900">Confirmed Duplicate</p>
                  <p className="text-xs text-gray-600 mt-1">
                    Both payments charged the same single order; flag extra capture for separate manual refund approval.
                  </p>
                </button>

                <button
                  type="button"
                  onClick={() => {
                    setSelectedDecision('SEPARATE_ORDERS')
                    setFormValidationError(null)
                  }}
                  className={`text-left p-3.5 rounded-lg border-2 transition-colors ${
                    selectedDecision === 'SEPARATE_ORDERS'
                      ? 'border-emerald-600 bg-emerald-50'
                      : 'border-gray-200 hover:border-gray-300 bg-white'
                  }`}
                >
                  <p className="text-sm font-semibold text-gray-900">Separate Orders</p>
                  <p className="text-xs text-gray-600 mt-1">
                    Distinct valid purchases; suppress dashboard alert while keeping this pair accessible for override.
                  </p>
                </button>
              </div>
              {isSameAsCurrent && (
                <p className="text-xs text-amber-800 bg-amber-50 border border-amber-200 rounded px-2.5 py-1.5">
                  This pair is already marked <strong>{currentDecision}</strong>. Select the opposite
                  decision to submit an override.
                </p>
              )}
            </div>

            {isReversal && (
              <div className="space-y-1.5">
                <label
                  htmlFor="override-reason-input"
                  className="block text-xs font-semibold uppercase tracking-wide text-amber-900"
                >
                  Override Reason (Required when changing {currentDecision} → {selectedDecision})
                </label>
                <textarea
                  id="override-reason-input"
                  rows={2}
                  className="input border-amber-300 focus:ring-amber-500"
                  placeholder="Explain why the prior review decision is being reversed…"
                  value={overrideReason}
                  onChange={(e) => setOverrideReason(e.target.value)}
                />
              </div>
            )}

            <div className="space-y-1.5">
              <div className="flex items-center justify-between gap-2 flex-wrap">
                <label
                  htmlFor="reviewer-input"
                  className="block text-xs font-semibold uppercase tracking-wide text-gray-600"
                >
                  Reviewer Name or Email (Optional)
                </label>
                <ReviewerIdentityBadge verified={false} />
              </div>
              <input
                id="reviewer-input"
                type="text"
                className="input"
                placeholder="e.g., alice.ops@merchant.com"
                value={reviewer}
                onChange={(e) => setReviewer(e.target.value)}
              />
              <p className="text-[11px] text-gray-500">
                Authentication is not enabled yet; self-entered reviewer names are recorded with{' '}
                <code>reviewer_verified: false</code>.
              </p>
            </div>

            <div className="space-y-1.5">
              <label
                htmlFor="review-notes-input"
                className="block text-xs font-semibold uppercase tracking-wide text-gray-600"
              >
                Investigation Notes (Optional)
              </label>
              <textarea
                id="review-notes-input"
                rows={3}
                className="input"
                placeholder="Document fulfillment checks, customer contact, or order evidence…"
                value={notes}
                onChange={(e) => setNotes(e.target.value)}
              />
            </div>

            <div className="flex items-center justify-between gap-3 pt-2">
              <span className="text-xs text-gray-500 flex items-center gap-1">
                <ShieldExclamationIcon className="w-4 h-4 text-gray-400" />
                Logs audit event to both transactions
              </span>
              <button
                type="submit"
                className="btn-primary"
                disabled={submitting || !selectedDecision || isSameAsCurrent}
              >
                {submitting
                  ? 'Saving Decision…'
                  : isReversal
                  ? 'Submit Override'
                  : 'Submit Decision'}
              </button>
            </div>
          </form>
        </div>

        {/* Decision History */}
        <div className="card space-y-4">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2">
              <ClockIcon className="w-5 h-5 text-gray-700" />
              <h2 className="text-base font-semibold text-gray-900">
                Review Decision History ({history.length})
              </h2>
            </div>
            <span className="text-xs text-gray-500">Previous entries preserved</span>
          </div>

          {history.length === 0 ? (
            <div className="rounded-lg border border-dashed border-gray-300 p-6 text-center text-sm text-gray-500">
              No review decisions recorded yet. Current state is{' '}
              <strong>PENDING_REVIEW</strong>.
            </div>
          ) : (
            <ol className="space-y-3">
              {history.map((entry, idx) => (
                <li
                  key={`${entry.recorded_at}-${idx}`}
                  className="rounded-lg border border-gray-200 bg-gray-50 p-4 space-y-2"
                >
                  <div className="flex items-center justify-between gap-2 flex-wrap">
                    <div className="flex items-center gap-2 flex-wrap">
                      <DecisionPill decision={entry.decision} />
                      {entry.previous_decision && (
                        <span className="text-xs text-gray-500">
                          from {entry.previous_decision.replace(/_/g, ' ')}
                        </span>
                      )}
                    </div>
                    <span className="text-xs text-gray-500">
                      {formatTimestamp(entry.recorded_at)}
                    </span>
                  </div>

                  <div className="flex items-center gap-2 flex-wrap text-xs text-gray-700">
                    <span>
                      <strong>Reviewer:</strong> {entry.reviewer || 'unauthenticated-agent'}
                    </span>
                    <ReviewerIdentityBadge verified={Boolean(entry.reviewer_verified)} />
                  </div>

                  {entry.override_reason && (
                    <p className="text-xs text-amber-900 bg-amber-50 border border-amber-200 rounded px-2.5 py-1.5">
                      <strong>Override Reason:</strong> {entry.override_reason}
                    </p>
                  )}

                  {entry.notes && (
                    <p className="text-xs text-gray-700">
                      <strong>Notes:</strong> {entry.notes}
                    </p>
                  )}
                </li>
              ))}
            </ol>
          )}
        </div>
      </div>
    </div>
  )
}
