import React, { useEffect, useState, useRef } from 'react'
import { useParams, useNavigate, Link } from 'react-router-dom'
import { format, parseISO } from 'date-fns'
import {
  ArrowLeftIcon,
  ArrowUpCircleIcon,
  CheckCircleIcon,
  PaperClipIcon,
  XMarkIcon,
} from '@heroicons/react/24/outline'
import StatusBadge from '../components/StatusBadge'
import PriorityBadge from '../components/PriorityBadge'
import AIRecommendationPanel from '../components/AIRecommendationPanel'
import { disputeService, aiService } from '../services/api'
import toast from 'react-hot-toast'

function DetailRow({ label, value }) {
  return (
    <div>
      <dt className="text-xs font-semibold text-gray-500 uppercase tracking-wide">{label}</dt>
      <dd className="mt-1 text-sm text-gray-900 font-medium break-all">{value ?? '—'}</dd>
    </div>
  )
}

// ─── Escalate Modal ───────────────────────────────────────────────────────────
function EscalateModal({ onClose, onConfirm, loading }) {
  const [note, setNote] = useState('')
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div className="bg-white rounded-xl shadow-xl w-full max-w-md p-6 space-y-4">
        <div className="flex items-center justify-between">
          <h3 className="text-base font-semibold text-gray-900">Escalate Dispute</h3>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600">
            <XMarkIcon className="w-5 h-5" />
          </button>
        </div>
        <p className="text-sm text-gray-600">
          Provide a note explaining why this dispute needs escalation.
        </p>
        <textarea
          className="input h-28 resize-none"
          placeholder="Escalation reason…"
          value={note}
          onChange={(e) => setNote(e.target.value)}
        />
        <div className="flex justify-end gap-3">
          <button className="btn-secondary" onClick={onClose} disabled={loading}>Cancel</button>
          <button
            className="btn-danger"
            onClick={() => onConfirm(note)}
            disabled={loading || !note.trim()}
          >
            {loading ? 'Escalating…' : 'Escalate'}
          </button>
        </div>
      </div>
    </div>
  )
}

export default function DisputeDetail() {
  const { id } = useParams()
  const navigate = useNavigate()

  const [dispute, setDispute] = useState(null)
  const [aiRec, setAiRec] = useState(null)
  const [aiLoading, setAiLoading] = useState(false)
  const [aiError, setAiError] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  const [evidenceText, setEvidenceText] = useState('')
  const [submittingEvidence, setSubmittingEvidence] = useState(false)
  const [accepting, setAccepting] = useState(false)
  const [escalateOpen, setEscalateOpen] = useState(false)
  const [escalating, setEscalating] = useState(false)

  useEffect(() => {
    async function fetchDispute() {
      setLoading(true)
      setError(null)
      try {
        const res = await disputeService.getById(id)
        setDispute(res.data)
        // Kick off AI analysis
        fetchAI(res.data)
      } catch (err) {
        setError(err.userMessage ?? 'Dispute not found')
      } finally {
        setLoading(false)
      }
    }
    fetchDispute()
  }, [id])

  async function fetchAI(disputeData) {
    setAiLoading(true)
    setAiError(null)
    try {
      const res = await aiService.analyze({
        dispute_id: disputeData?.id ?? id,
        transaction_id: disputeData?.transaction_id,
      })
      setAiRec(res.data)
    } catch (err) {
      setAiError(err.userMessage ?? 'AI analysis failed')
    } finally {
      setAiLoading(false)
    }
  }

  async function handleAccept() {
    if (!window.confirm('Accept this dispute claim? This will initiate a refund.')) return
    setAccepting(true)
    try {
      await disputeService.accept(id)
      toast.success('Claim accepted — refund recorded')
      setDispute((prev) => ({ ...prev, status: 'RESOLVED' }))
    } catch (err) {
      toast.error(err.userMessage ?? 'Failed to accept claim')
    } finally {
      setAccepting(false)
    }
  }

  async function handleEscalate(note) {
    setEscalating(true)
    try {
      await disputeService.escalate(id, note)
      toast.success('Dispute escalated')
      setDispute((prev) => ({ ...prev, status: 'UNDER_REVIEW' }))
      setEscalateOpen(false)
    } catch (err) {
      toast.error(err.userMessage ?? 'Failed to escalate dispute')
    } finally {
      setEscalating(false)
    }
  }

  async function handleSubmitEvidence(e) {
    e.preventDefault()
    if (!evidenceText.trim()) {
      toast.error('Please enter evidence description')
      return
    }
    setSubmittingEvidence(true)
    try {
      await disputeService.submitEvidence(id, { description: evidenceText })
      toast.success('Evidence submitted successfully')
      setEvidenceText('')
      setDispute((prev) => ({ ...prev, evidence_submitted: true }))
    } catch (err) {
      toast.error(err.userMessage ?? 'Failed to submit evidence')
    } finally {
      setSubmittingEvidence(false)
    }
  }

  if (loading) {
    return (
      <div className="p-8 space-y-6">
        <div className="h-6 bg-gray-200 rounded w-32 animate-pulse" />
        <div className="card space-y-4">
          {Array.from({ length: 5 }).map((_, i) => (
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

  const isResolved = ['RESOLVED', 'CLOSED'].includes(dispute?.status?.toUpperCase())

  return (
    <>
      {escalateOpen && (
        <EscalateModal
          onClose={() => setEscalateOpen(false)}
          onConfirm={handleEscalate}
          loading={escalating}
        />
      )}

      <div className="p-8 space-y-6">
        {/* Header */}
        <div className="flex items-center gap-4">
          <button className="btn-secondary" onClick={() => navigate(-1)}>
            <ArrowLeftIcon className="w-4 h-4" /> Back
          </button>
          <div className="flex-1">
            <div className="flex items-center gap-3">
              <h1 className="text-xl font-bold text-gray-900">Dispute {dispute?.id}</h1>
              <StatusBadge status={dispute?.status} />
              <PriorityBadge priority={dispute?.priority} />
            </div>
            <p className="text-sm text-gray-500 mt-0.5">Dispute Details</p>
          </div>
        </div>

        <div className="grid grid-cols-1 xl:grid-cols-3 gap-6">
          {/* Left — info + evidence + actions */}
          <div className="xl:col-span-2 space-y-6">
            {/* Dispute info card */}
            <div className="card">
              <h2 className="text-base font-semibold text-gray-800 mb-4">Dispute Information</h2>
              <dl className="grid grid-cols-1 sm:grid-cols-2 gap-5">
                <DetailRow label="Dispute ID" value={dispute?.id} />
                <DetailRow label="Reason" value={dispute?.reason?.replace(/_/g, ' ')} />
                <DetailRow
                  label="Transaction ID"
                  value={
                    dispute?.transaction_id ? (
                      <Link
                        to={`/transactions/${dispute.transaction_id}`}
                        className="text-[#003087] hover:underline"
                      >
                        {dispute.transaction_id}
                      </Link>
                    ) : undefined
                  }
                />
                <DetailRow
                  label="Evidence Submitted"
                  value={dispute?.evidence_submitted ? 'Yes' : 'No'}
                />
                <DetailRow label="Resolution" value={dispute?.resolution} />
                <DetailRow
                  label="Created"
                  value={
                    dispute?.created_at
                      ? format(parseISO(dispute.created_at), 'PPP p')
                      : undefined
                  }
                />
                <DetailRow
                  label="Updated"
                  value={
                    dispute?.updated_at
                      ? format(parseISO(dispute.updated_at), 'PPP p')
                      : undefined
                  }
                />
                {dispute?.resolution_case_id && (
                  <DetailRow
                    label="Resolution Case"
                    value={
                      <Link
                        to={`/cases/${dispute.resolution_case_id}`}
                        className="text-[#003087] hover:underline"
                      >
                        {dispute.resolution_case_id}
                      </Link>
                    }
                  />
                )}
              </dl>

              {/* Action buttons */}
              {!isResolved && (
                <div className="flex flex-wrap gap-3 mt-6 pt-5 border-t border-gray-100">
                  <button
                    className="btn-primary"
                    onClick={handleAccept}
                    disabled={accepting}
                  >
                    <CheckCircleIcon className="w-4 h-4" />
                    {accepting ? 'Processing…' : 'Accept Claim'}
                  </button>
                  <button
                    className="btn-danger"
                    onClick={() => setEscalateOpen(true)}
                  >
                    <ArrowUpCircleIcon className="w-4 h-4" />
                    Escalate
                  </button>
                </div>
              )}
            </div>

            {/* Evidence submission */}
            {!isResolved && (
              <div className="card">
                <h2 className="text-base font-semibold text-gray-800 mb-4">Submit Evidence</h2>
                <form onSubmit={handleSubmitEvidence} className="space-y-4">
                  <div>
                    <label className="block text-sm text-gray-600 mb-1.5">
                      Evidence Description
                    </label>
                    <textarea
                      className="input h-28 resize-none"
                      placeholder="Describe the evidence supporting your position…"
                      value={evidenceText}
                      onChange={(e) => setEvidenceText(e.target.value)}
                    />
                  </div>
                  <div className="flex items-center gap-2 text-sm text-gray-500">
                    <PaperClipIcon className="w-4 h-4" />
                    <span>Attach files via the backend API or upload portal.</span>
                  </div>
                  <button
                    type="submit"
                    className="btn-primary"
                    disabled={submittingEvidence}
                  >
                    {submittingEvidence ? 'Submitting…' : 'Submit Evidence'}
                  </button>
                </form>
              </div>
            )}
          </div>

          {/* Right — AI panel */}
          <div>
            <AIRecommendationPanel
              recommendation={aiRec}
              isLoading={aiLoading}
              error={aiError}
            />
          </div>
        </div>
      </div>
    </>
  )
}
