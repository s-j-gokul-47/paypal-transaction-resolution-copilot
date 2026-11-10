import React, { useEffect, useState } from 'react'
import { useParams, useNavigate, Link } from 'react-router-dom'
import { format, parseISO, isPast, parseJSON } from 'date-fns'
import {
  ArrowLeftIcon,
  ExclamationCircleIcon,
  PlusIcon,
  TrashIcon,
  CheckCircleIcon,
} from '@heroicons/react/24/outline'
import StatusBadge from '../components/StatusBadge'
import PriorityBadge from '../components/PriorityBadge'
import { caseService } from '../services/api'
import toast from 'react-hot-toast'

function DetailRow({ label, value }) {
  return (
    <div>
      <dt className="text-xs font-semibold text-gray-500 uppercase tracking-wide">{label}</dt>
      <dd className="mt-1 text-sm text-gray-900 font-medium break-all">{value ?? '—'}</dd>
    </div>
  )
}

export default function CaseDetail() {
  const { id } = useParams()
  const navigate = useNavigate()

  const [caseData, setCaseData] = useState(null)
  const [summary, setSummary] = useState(null)
  const [summaryLoading, setSummaryLoading] = useState(false)
  const [summaryError, setSummaryError] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  const [steps, setSteps] = useState([])
  const [newStep, setNewStep] = useState('')
  const [outcomeText, setOutcomeText] = useState('')
  const [savingOutcome, setSavingOutcome] = useState(false)

  useEffect(() => {
    async function fetchCase() {
      setLoading(true)
      setError(null)
      try {
        const res = await caseService.getById(id)
        const data = res.data
        setCaseData(data)
        setOutcomeText(data?.outcome ?? '')
        setSteps(data?.steps_taken ?? [])
        fetchSummary()
      } catch (err) {
        setError(err.userMessage ?? 'Case not found')
      } finally {
        setLoading(false)
      }
    }
    fetchCase()
  }, [id])

  async function fetchSummary() {
    setSummaryLoading(true)
    setSummaryError(null)
    try {
      const res = await caseService.getSummary(id)
      setSummary(res.data)
    } catch (err) {
      setSummaryError(err.userMessage ?? 'Could not load AI summary')
    } finally {
      setSummaryLoading(false)
    }
  }

  function addStep() {
    const trimmed = newStep.trim()
    if (!trimmed) return
    const stepObj = {
      timestamp: new Date().toISOString(),
      action: 'AGENT_STEP',
      notes: trimmed,
      agent: caseData?.assigned_agent || 'support.agent',
    }
    setSteps((prev) => [...prev, stepObj])
    setNewStep('')
  }

  function removeStep(idx) {
    setSteps((prev) => prev.filter((_, i) => i !== idx))
  }

  async function handleSaveOutcome(e) {
    e.preventDefault()
    if (!outcomeText.trim()) {
      toast.error('Please enter an outcome')
      return
    }
    setSavingOutcome(true)
    try {
      const res = await caseService.update(id, {
        outcome: outcomeText,
        steps_taken: steps,
      })
      toast.success('Case updated successfully')
      setCaseData(res.data)
      fetchSummary()
    } catch (err) {
      toast.error(err.userMessage ?? 'Failed to update case')
    } finally {
      setSavingOutcome(false)
    }
  }

  const slaBreach = caseData?.sla_due_at
    ? isPast(parseJSON(caseData.sla_due_at)) && !['RESOLVED', 'CLOSED', 'REFUNDED'].includes(caseData?.outcome?.toUpperCase())
    : caseData?.sla_breached

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

  return (
    <div className="p-8 space-y-6">
      {/* Header */}
      <div className="flex items-center gap-4">
        <button className="btn-secondary" onClick={() => navigate(-1)}>
          <ArrowLeftIcon className="w-4 h-4" /> Back
        </button>
        <div className="flex-1">
          <div className="flex items-center gap-3 flex-wrap">
            <h1 className="text-xl font-bold text-gray-900">Case {caseData?.id}</h1>
            <PriorityBadge priority={caseData?.priority} />
            <StatusBadge status={caseData?.outcome ?? 'OPEN'} />
          </div>
          <p className="text-sm text-gray-500 mt-0.5">
            Resolution Case Details {caseData?.sla_info ? `• SLA: ${caseData.sla_info}` : ''}
          </p>
        </div>
      </div>

      {/* SLA Breach Warning */}
      {slaBreach && (
        <div className="flex items-start gap-3 bg-red-50 border border-red-200 rounded-xl p-4">
          <ExclamationCircleIcon className="w-5 h-5 text-red-500 flex-shrink-0 mt-0.5" />
          <div>
            <p className="text-sm font-semibold text-red-800">SLA Deadline Breached</p>
            <p className="text-sm text-red-600">
              {caseData?.sla_info || 'This case has exceeded its SLA deadline.'}
            </p>
          </div>
        </div>
      )}

      <div className="grid grid-cols-1 xl:grid-cols-3 gap-6">
        <div className="xl:col-span-2 space-y-6">
          {/* Case info */}
          <div className="card">
            <h2 className="text-base font-semibold text-gray-800 mb-4">Case Information</h2>
            <dl className="grid grid-cols-1 sm:grid-cols-2 gap-5">
              <DetailRow label="Case ID" value={caseData?.id} />
              <DetailRow label="Assigned Agent" value={caseData?.assigned_agent} />
              <DetailRow label="Priority" value={caseData?.priority} />
              <DetailRow
                label="Dispute"
                value={
                  caseData?.dispute_id ? (
                    <Link
                      to={`/disputes/${caseData.dispute_id}`}
                      className="text-[#003087] hover:underline"
                    >
                      {caseData.dispute_id}
                    </Link>
                  ) : undefined
                }
              />
              <DetailRow label="Outcome" value={caseData?.outcome} />
              <DetailRow label="SLA Status" value={caseData?.sla_info} />
              <DetailRow
                label="Created"
                value={caseData?.created_at ? format(parseISO(caseData.created_at), 'PPP p') : undefined}
              />
              <DetailRow
                label="Updated"
                value={caseData?.updated_at ? format(parseISO(caseData.updated_at), 'PPP p') : undefined}
              />
            </dl>
          </div>

          {/* Steps taken timeline */}
          <div className="card">
            <h2 className="text-base font-semibold text-gray-800 mb-4">Steps Taken</h2>
            {steps.length === 0 ? (
              <p className="text-sm text-gray-400 mb-4">No steps recorded yet.</p>
            ) : (
              <ol className="relative border-l-2 border-gray-200 ml-3 space-y-4 mb-4">
                {steps.map((step, idx) => {
                  const isObj = step && typeof step === 'object'
                  const title = isObj ? step.action || 'STEP' : `Step ${idx + 1}`
                  const notes = isObj ? step.notes || JSON.stringify(step) : String(step)
                  const meta = isObj
                    ? [step.agent, step.timestamp ? format(parseISO(step.timestamp), 'MMM d, HH:mm') : null]
                        .filter(Boolean)
                        .join(' • ')
                    : null

                  return (
                    <li key={idx} className="ml-6 flex items-start justify-between gap-3">
                      <div>
                        <span className="absolute -left-[9px] flex items-center justify-center w-4 h-4 rounded-full bg-[#003087] text-white text-[10px] font-bold">
                          {idx + 1}
                        </span>
                        <p className="text-xs font-semibold text-[#003087] uppercase tracking-wide">
                          {title}
                        </p>
                        <p className="text-sm text-gray-700">{notes}</p>
                        {meta && <p className="text-xs text-gray-400 mt-0.5">{meta}</p>}
                      </div>
                      <button
                        type="button"
                        className="flex-shrink-0 text-gray-400 hover:text-red-500 transition-colors"
                        onClick={() => removeStep(idx)}
                        title="Remove step"
                      >
                        <TrashIcon className="w-4 h-4" />
                      </button>
                    </li>
                  )
                })}
              </ol>
            )}

            <div className="flex gap-2">
              <input
                type="text"
                className="input flex-1"
                placeholder="Add a new step…"
                value={newStep}
                onChange={(e) => setNewStep(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && (e.preventDefault(), addStep())}
              />
              <button type="button" className="btn-secondary" onClick={addStep}>
                <PlusIcon className="w-4 h-4" />
                Add
              </button>
            </div>
          </div>

          {/* Outcome form */}
          <div className="card">
            <h2 className="text-base font-semibold text-gray-800 mb-4">Record Outcome</h2>
            <form onSubmit={handleSaveOutcome} className="space-y-4">
              <div>
                <label className="block text-sm text-gray-600 mb-1.5">
                  Outcome / Resolution Notes
                </label>
                <textarea
                  className="input h-32 resize-none"
                  placeholder="Describe the outcome and resolution taken…"
                  value={outcomeText}
                  onChange={(e) => setOutcomeText(e.target.value)}
                />
              </div>
              <button type="submit" className="btn-primary" disabled={savingOutcome}>
                <CheckCircleIcon className="w-4 h-4" />
                {savingOutcome ? 'Saving…' : 'Save Outcome'}
              </button>
            </form>
          </div>
        </div>

        {/* Right — Case Summary */}
        <div>
          <div className="bg-white rounded-xl border border-gray-200 shadow-sm p-6 space-y-4">
            <div className="flex items-center gap-2">
              <span className="text-lg">🤖</span>
              <h3 className="text-base font-semibold text-gray-900">AI Case Summary</h3>
            </div>

            {summaryLoading ? (
              <div className="space-y-3 animate-pulse">
                <div className="h-4 bg-gray-200 rounded w-full" />
                <div className="h-4 bg-gray-200 rounded w-5/6" />
                <div className="h-4 bg-gray-200 rounded w-4/6" />
              </div>
            ) : summaryError ? (
              <div className="bg-red-50 rounded-lg p-3 text-sm text-red-600">
                {summaryError}
              </div>
            ) : summary ? (
              <div className="space-y-4">
                {summary.summary_markdown && (
                  <pre className="whitespace-pre-wrap text-xs text-gray-700 bg-gray-50 border border-gray-200 rounded-lg p-3 font-sans leading-relaxed">
                    {summary.summary_markdown}
                  </pre>
                )}
                {summary.next_actions?.length > 0 && (
                  <div>
                    <p className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-1.5">
                      Suggested Next Actions
                    </p>
                    <ul className="list-disc list-inside text-sm text-gray-700 space-y-1">
                      {summary.next_actions.map((act, i) => (
                        <li key={i}>{act}</li>
                      ))}
                    </ul>
                  </div>
                )}
              </div>
            ) : (
              <p className="text-sm text-gray-400">No summary available.</p>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
