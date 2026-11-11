import React, { useEffect, useState, useCallback } from 'react'
import { useParams, useNavigate, Link } from 'react-router-dom'
import { format, parseISO } from 'date-fns'
import {
  ArrowLeftIcon,
  ArrowPathIcon,
  ExclamationTriangleIcon,
  DocumentDuplicateIcon,
  ClipboardDocumentIcon,
  SparklesIcon,
  CheckBadgeIcon,
} from '@heroicons/react/24/outline'
import StatusBadge from '../components/StatusBadge'
import { transactionService } from '../services/api'
import toast from 'react-hot-toast'

function DetailRow({ label, value }) {
  return (
    <div className="sm:col-span-1">
      <dt className="text-xs font-semibold text-gray-500 uppercase tracking-wide">{label}</dt>
      <dd className="mt-1 text-sm text-gray-900 font-medium break-all">{value ?? '—'}</dd>
    </div>
  )
}

function eventDotColor(eventType) {
  switch (eventType) {
    case 'PAYMENT_CAPTURED':
      return 'bg-green-500'
    case 'PAYMENT_PENDING':
      return 'bg-amber-500'
    case 'PAYMENT_FAILED':
      return 'bg-red-500'
    case 'REFUND_ISSUED':
      return 'bg-blue-500'
    case 'DISPUTE_OPENED':
    case 'DISPUTE_ESCALATED':
      return 'bg-orange-500'
    case 'DUPLICATE_REVIEWED':
      return 'bg-indigo-500'
    default:
      return 'bg-[#003087]'
  }
}

export default function TransactionDetail() {
  const { id } = useParams()
  const navigate = useNavigate()

  const [resolution, setResolution] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [refunding, setRefunding] = useState(false)

  // Support summary generator state
  const [audience, setAudience] = useState('internal')
  const [agentNotes, setAgentNotes] = useState('')
  const [supportSummary, setSupportSummary] = useState(null)
  const [summaryLoading, setSummaryLoading] = useState(false)

  const fetchResolution = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await transactionService.getResolution(id)
      setResolution(res.data)
    } catch (err) {
      setError(err.userMessage ?? 'Transaction not found')
    } finally {
      setLoading(false)
    }
  }, [id])

  useEffect(() => {
    fetchResolution()
  }, [fetchResolution])

  async function handleRefund() {
    if (!window.confirm('Issue a full refund for this transaction?')) return
    setRefunding(true)
    try {
      const res = await transactionService.refund(id, {
        reason: 'Refunded from Transaction Resolution Copilot',
      })
      const isSim = res.data?.paypal_response?.is_simulated
      toast.success(
        isSim
          ? 'Refund recorded (Simulated Mock Mode — no live PayPal charge moved)'
          : 'Refund issued via PayPal API'
      )
      await fetchResolution()
    } catch (err) {
      toast.error(err.userMessage ?? 'Failed to issue refund')
    } finally {
      setRefunding(false)
    }
  }

  async function handleGenerateSummary(e) {
    e.preventDefault()
    setSummaryLoading(true)
    try {
      const res = await transactionService.generateSupportSummary(id, {
        audience,
        agent_notes: agentNotes.trim() || undefined,
      })
      setSupportSummary(res.data)
    } catch (err) {
      toast.error(err.userMessage ?? 'Failed to generate support summary')
    } finally {
      setSummaryLoading(false)
    }
  }

  function handleCopySummary() {
    if (!supportSummary?.summary) return
    navigator.clipboard.writeText(supportSummary.summary)
    toast.success('Support summary copied to clipboard')
  }

  if (loading) {
    return (
      <div className="p-8 space-y-6">
        <div className="h-6 bg-gray-200 rounded w-32 animate-pulse" />
        <div className="card space-y-4">
          {Array.from({ length: 6 }).map((_, i) => (
            <div key={i} className="h-5 bg-gray-200 rounded animate-pulse w-3/4" />
          ))}
        </div>
      </div>
    )
  }

  if (error) {
    return (
      <div className="p-8">
        <button className="btn-secondary mb-6" onClick={() => navigate(-1)}>
          <ArrowLeftIcon className="w-4 h-4" /> Back
        </button>
        <div className="card text-center py-10">
          <p className="text-red-600 font-medium">{error}</p>
        </div>
      </div>
    )
  }

  const txn = resolution?.transaction
  const timeline = resolution?.timeline ?? []
  const duplicates = resolution?.duplicates?.candidates ?? []
  const hasActiveDuplicate = resolution?.duplicates?.has_possible_duplicates
  const explanation = resolution?.explanation
  const disputes = resolution?.disputes ?? []
  const paypalMode = resolution?.paypal_mode

  return (
    <div className="p-8 space-y-6">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div className="flex items-center gap-4">
          <button className="btn-secondary" onClick={() => navigate(-1)}>
            <ArrowLeftIcon className="w-4 h-4" /> Back
          </button>
          <div>
            <div className="flex items-center gap-3 flex-wrap">
              <h1 className="text-xl font-bold text-gray-900">
                {txn?.paypal_transaction_id ?? txn?.id}
              </h1>
              <StatusBadge status={txn?.status} />
              {hasActiveDuplicate && (
                <span className="inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full text-xs font-semibold bg-amber-100 text-amber-800 border border-amber-300">
                  <DocumentDuplicateIcon className="w-3.5 h-3.5" />
                  Possible Duplicate
                </span>
              )}
            </div>
            <p className="text-xs text-gray-500 mt-0.5">
              Internal ID: {txn?.id} • PayPal Environment: {paypalMode}
            </p>
          </div>
        </div>

        <div className="flex gap-3">
          {txn?.status === 'COMPLETED' && (
            <button
              className="btn-primary"
              onClick={handleRefund}
              disabled={refunding}
            >
              <ArrowPathIcon className="w-4 h-4" />
              {refunding
                ? 'Processing…'
                : paypalMode === 'mock'
                ? 'Issue Refund (Mock)'
                : 'Issue Refund'}
            </button>
          )}
          {disputes.length > 0 && (
            <Link to={`/disputes/${disputes[0].id}`} className="btn-secondary">
              <ExclamationTriangleIcon className="w-4 h-4" />
              View Linked Dispute
            </Link>
          )}
        </div>
      </div>

      {/* Main 2-column Resolution Layout */}
      <div className="grid grid-cols-1 xl:grid-cols-3 gap-6">
        {/* Left 2 columns: State Explanation + Duplicate Check + Timeline + Record Details */}
        <div className="xl:col-span-2 space-y-6">
          {/* 1. Transaction State Explanation (Grounded) */}
          {explanation && (
            <div className="card space-y-4 border-l-4 border-l-[#003087]">
              <div className="flex items-center justify-between gap-2 flex-wrap">
                <div className="flex items-center gap-2">
                  <SparklesIcon className="w-5 h-5 text-[#003087]" />
                  <h2 className="text-base font-semibold text-gray-900">
                    Transaction State Explanation
                  </h2>
                </div>
                <span className="inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full text-xs font-medium bg-blue-50 text-[#003087] border border-blue-200">
                  <CheckBadgeIcon className="w-3.5 h-3.5" />
                  Grounded ({explanation.mode})
                </span>
              </div>

              <div>
                <p className="text-sm font-semibold text-gray-900">{explanation.headline}</p>
                <p className="text-sm text-gray-700 mt-1 leading-relaxed">
                  {explanation.plain_english_summary}
                </p>
              </div>

              <div className="grid grid-cols-1 md:grid-cols-2 gap-4 pt-2">
                <div className="bg-gray-50 rounded-lg p-3 border border-gray-100">
                  <p className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-1.5">
                    Observed Causes
                  </p>
                  <ul className="list-disc list-inside text-sm text-gray-700 space-y-1">
                    {(explanation.likely_causes ?? []).map((c, i) => (
                      <li key={i}>{c}</li>
                    ))}
                  </ul>
                </div>
                <div className="bg-blue-50/50 rounded-lg p-3 border border-blue-100">
                  <p className="text-xs font-semibold text-[#003087] uppercase tracking-wide mb-1.5">
                    Recommended Next Steps
                  </p>
                  <ul className="list-disc list-inside text-sm text-gray-700 space-y-1">
                    {(explanation.recommended_actions ?? []).map((a, i) => (
                      <li key={i}>{a}</li>
                    ))}
                  </ul>
                </div>
              </div>

              {explanation.verified_fields_used?.length > 0 && (
                <div className="pt-2 border-t border-gray-100 flex flex-wrap items-center gap-1.5">
                  <span className="text-xs text-gray-500 mr-1">Verified fields cited:</span>
                  {explanation.verified_fields_used.map((f, i) => (
                    <span
                      key={i}
                      className="px-2 py-0.5 rounded bg-gray-100 text-gray-700 text-[11px] font-mono"
                    >
                      {f}
                    </span>
                  ))}
                </div>
              )}
            </div>
          )}

          {/* 2. Possible Duplicate Payments */}
          <div className="card space-y-3">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <DocumentDuplicateIcon className="w-5 h-5 text-gray-700" />
                <h2 className="text-base font-semibold text-gray-900">Duplicate Payment Check</h2>
              </div>
              <span className="text-xs text-gray-500">24-hour window • Buyer + Merchant + Amount</span>
            </div>

            {duplicates.length === 0 ? (
              <p className="text-sm text-green-700 bg-green-50 border border-green-200 rounded-lg p-3">
                No matching duplicate or retry payments found within 24 hours.
              </p>
            ) : (
              <div className="space-y-3">
                {duplicates.map((dup) => {
                  const reviewDecision = dup.review_decision ?? 'PENDING_REVIEW'
                  const cardTone =
                    reviewDecision === 'CONFIRMED_DUPLICATE'
                      ? 'bg-red-50/60 border-red-200'
                      : reviewDecision === 'SEPARATE_ORDERS'
                      ? 'bg-gray-50 border-gray-200'
                      : dup.classification === 'POSSIBLE_DUPLICATE'
                      ? 'bg-amber-50 border-amber-200'
                      : 'bg-gray-50 border-gray-200'

                  const reviewPillTone =
                    reviewDecision === 'CONFIRMED_DUPLICATE'
                      ? 'bg-red-100 text-red-800 border-red-300'
                      : reviewDecision === 'SEPARATE_ORDERS'
                      ? 'bg-emerald-100 text-emerald-800 border-emerald-300'
                      : 'bg-amber-100 text-amber-800 border-amber-300'

                  return (
                    <div
                      key={dup.transaction_id}
                      className={`rounded-lg border p-4 space-y-2.5 ${cardTone}`}
                    >
                      <div className="flex items-center justify-between flex-wrap gap-2">
                        <div className="flex items-center gap-2 flex-wrap">
                          <Link
                            to={`/transactions/${dup.transaction_id}`}
                            className="text-sm font-semibold text-[#003087] hover:underline"
                          >
                            {dup.paypal_transaction_id}
                          </Link>
                          <StatusBadge status={dup.status} />
                          {dup.role && (
                            <span className="px-2 py-0.5 rounded-full text-[11px] font-semibold bg-blue-50 border border-blue-200 text-[#003087]">
                              {dup.role}
                            </span>
                          )}
                          <span className="px-2 py-0.5 rounded-full text-xs font-semibold bg-white border border-gray-300 text-gray-700">
                            {dup.classification.replace(/_/g, ' ')} • {dup.confidence}
                          </span>
                          <span
                            className={`px-2 py-0.5 rounded-full text-xs font-semibold border ${reviewPillTone}`}
                          >
                            {reviewDecision.replace(/_/g, ' ')}
                          </span>
                        </div>
                        <span className="text-sm font-semibold text-gray-900">
                          {Number(dup.amount).toFixed(2)} {dup.currency}
                        </span>
                      </div>

                      {(dup.order_id || dup.description || dup.order_relationship) && (
                        <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-gray-600">
                          {dup.order_id && (
                            <span>
                              <strong>Order ID:</strong> {dup.order_id}
                            </span>
                          )}
                          {dup.order_relationship && (
                            <span>
                              <strong>Order Comparison:</strong>{' '}
                              {dup.order_relationship.replace(/_/g, ' ')}
                            </span>
                          )}
                          {dup.description && (
                            <span>
                              <strong>Description:</strong> {dup.description}
                            </span>
                          )}
                        </div>
                      )}

                      <p className="text-sm text-gray-700">{dup.explanation}</p>
                      <div className="flex items-center justify-between flex-wrap gap-3 pt-1">
                        <p className="text-xs text-gray-600">
                          <strong>Action:</strong> {dup.recommended_action}
                        </p>
                        <Link
                          to={`/transactions/${txn.id}/duplicates/${dup.transaction_id}`}
                          className="btn-secondary text-xs py-1.5 px-3"
                        >
                          {reviewDecision === 'PENDING_REVIEW'
                            ? 'Investigate & Review →'
                            : 'Inspect / Override Review →'}
                        </Link>
                      </div>
                    </div>
                  )
                })}
              </div>
            )}
          </div>

          {/* 3. Verified Payment Timeline */}
          <div className="card">
            <h2 className="text-base font-semibold text-gray-800 mb-4">
              Payment Timeline ({timeline.length} recorded events)
            </h2>
            {timeline.length === 0 ? (
              <p className="text-sm text-gray-400">No timeline events recorded yet.</p>
            ) : (
              <ol className="relative border-l-2 border-gray-200 ml-3 space-y-6">
                {timeline.map((event) => (
                  <li key={event.id} className="ml-6">
                    <span
                      className={`absolute -left-[9px] flex items-center justify-center w-4 h-4 rounded-full ${eventDotColor(
                        event.event_type
                      )}`}
                    />
                    <div className="flex items-center justify-between gap-2 flex-wrap">
                      <p className="text-sm font-semibold text-gray-800">{event.title}</p>
                      <span className="text-[11px] px-2 py-0.5 rounded bg-gray-100 text-gray-600 font-medium">
                        {event.source}
                      </span>
                    </div>
                    {event.description && (
                      <p className="text-sm text-gray-600 mt-0.5">{event.description}</p>
                    )}
                    <p className="text-xs text-gray-400 mt-0.5">
                      {event.occurred_at ? format(parseISO(event.occurred_at), 'PPP p') : '—'}
                    </p>
                  </li>
                ))}
              </ol>
            )}
          </div>

          {/* 4. Transaction Record Information */}
          <div className="card">
            <h2 className="text-base font-semibold text-gray-800 mb-4">Verified Transaction Record</h2>
            <dl className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-5">
              <DetailRow label="PayPal Transaction ID" value={txn?.paypal_transaction_id} />
              <DetailRow
                label="Amount"
                value={
                  txn?.amount !== undefined
                    ? `${Number(txn.amount).toLocaleString('en-US', { minimumFractionDigits: 2 })} ${txn.currency ?? 'USD'}`
                    : undefined
                }
              />
              <DetailRow label="Merchant ID" value={txn?.merchant_id} />
              <DetailRow label="Buyer Email" value={txn?.buyer_email} />
              <DetailRow label="Seller Email" value={txn?.seller_email} />
              <DetailRow
                label="Created"
                value={txn?.created_at ? format(parseISO(txn.created_at), 'PPP p') : undefined}
              />
              <DetailRow label="Description" value={txn?.description} />
              <DetailRow
                label="Metadata"
                value={
                  txn?.metadata && Object.keys(txn.metadata).length > 0
                    ? JSON.stringify(txn.metadata)
                    : 'None'
                }
              />
            </dl>
          </div>

          {/* 5. Associated Disputes (Secondary) */}
          <div className="card">
            <h2 className="text-base font-semibold text-gray-800 mb-4">Associated Disputes (Secondary)</h2>
            {disputes.length === 0 ? (
              <p className="text-sm text-gray-400">No disputes linked to this transaction.</p>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead className="bg-gray-50">
                    <tr>
                      <th className="table-header">PayPal Dispute ID</th>
                      <th className="table-header">Reason</th>
                      <th className="table-header">Status</th>
                      <th className="table-header">Created</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-gray-100">
                    {disputes.map((d) => (
                      <tr key={d.id} className="hover:bg-gray-50">
                        <td className="table-cell">
                          <Link
                            to={`/disputes/${d.id}`}
                            className="text-[#003087] font-medium hover:underline"
                          >
                            {d.paypal_dispute_id ?? d.id}
                          </Link>
                        </td>
                        <td className="table-cell">{d.reason?.replace(/_/g, ' ') ?? '—'}</td>
                        <td className="table-cell"><StatusBadge status={d.status} /></td>
                        <td className="table-cell text-gray-500">
                          {d.created_at ? format(parseISO(d.created_at), 'MMM d, yyyy') : '—'}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </div>

        {/* Right column: Support Summary Preparation */}
        <div className="space-y-6">
          <div className="card space-y-4">
            <div>
              <h2 className="text-base font-semibold text-gray-900">Prepare Support Summary</h2>
              <p className="text-xs text-gray-500 mt-0.5">
                Generates a draft grounded only in verified record fields, timeline events, and duplicate checks.
              </p>
            </div>

            <form onSubmit={handleGenerateSummary} className="space-y-3">
              <div>
                <label className="block text-xs font-semibold text-gray-600 mb-1">
                  Target Audience
                </label>
                <select
                  className="select"
                  value={audience}
                  onChange={(e) => setAudience(e.target.value)}
                >
                  <option value="internal">Internal Support Note</option>
                  <option value="buyer">Buyer Reply Draft</option>
                  <option value="escalation">Escalation Brief</option>
                </select>
              </div>

              <div>
                <label className="block text-xs font-semibold text-gray-600 mb-1">
                  Optional Agent Context
                </label>
                <textarea
                  className="input h-20 resize-none"
                  placeholder="e.g. Buyer contacted support asking why payment shows twice..."
                  value={agentNotes}
                  onChange={(e) => setAgentNotes(e.target.value)}
                />
              </div>

              <button
                type="submit"
                className="btn-primary w-full justify-center"
                disabled={summaryLoading}
              >
                {summaryLoading ? 'Generating…' : 'Generate Support Summary'}
              </button>
            </form>

            {supportSummary && (
              <div className="space-y-2 pt-3 border-t border-gray-100">
                <div className="flex items-center justify-between">
                  <span className="text-xs font-semibold text-gray-500 uppercase">
                    Draft ({supportSummary.mode})
                  </span>
                  <button
                    type="button"
                    onClick={handleCopySummary}
                    className="inline-flex items-center gap-1 text-xs font-semibold text-[#003087] hover:underline"
                  >
                    <ClipboardDocumentIcon className="w-4 h-4" />
                    Copy
                  </button>
                </div>
                <pre className="whitespace-pre-wrap text-xs text-gray-800 bg-gray-50 border border-gray-200 rounded-lg p-3 font-sans leading-relaxed">
                  {supportSummary.summary}
                </pre>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
