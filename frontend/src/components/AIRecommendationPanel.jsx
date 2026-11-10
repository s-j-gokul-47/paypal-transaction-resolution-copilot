import React from 'react'
import clsx from 'clsx'
import {
  ArrowPathIcon,
  ArrowUpCircleIcon,
  InformationCircleIcon,
  XCircleIcon,
  CheckCircleIcon,
} from '@heroicons/react/24/outline'

const actionConfig = {
  REFUND: {
    label: 'Issue Refund',
    className: 'bg-blue-100 text-blue-800 border-blue-300',
    Icon: ArrowPathIcon,
  },
  IMMEDIATE_REFUND: {
    label: 'Immediate Refund',
    className: 'bg-blue-100 text-blue-800 border-blue-300',
    Icon: ArrowPathIcon,
  },
  PROCESS_REFUND: {
    label: 'Process Refund',
    className: 'bg-blue-100 text-blue-800 border-blue-300',
    Icon: ArrowPathIcon,
  },
  PARTIAL_REFUND: {
    label: 'Partial Refund',
    className: 'bg-blue-100 text-blue-800 border-blue-300',
    Icon: ArrowPathIcon,
  },
  ESCALATE: {
    label: 'Escalate Case',
    className: 'bg-red-100 text-red-800 border-red-300',
    Icon: ArrowUpCircleIcon,
  },
  ESCALATE_FOR_REVIEW: {
    label: 'Escalate for Review',
    className: 'bg-red-100 text-red-800 border-red-300',
    Icon: ArrowUpCircleIcon,
  },
  REQUEST_INFO: {
    label: 'Request More Info',
    className: 'bg-yellow-100 text-yellow-800 border-yellow-300',
    Icon: InformationCircleIcon,
  },
  REQUEST_EVIDENCE: {
    label: 'Request Evidence',
    className: 'bg-yellow-100 text-yellow-800 border-yellow-300',
    Icon: InformationCircleIcon,
  },
  REJECT: {
    label: 'Reject Claim',
    className: 'bg-gray-100 text-gray-700 border-gray-300',
    Icon: XCircleIcon,
  },
  ACCEPT: {
    label: 'Accept Claim',
    className: 'bg-green-100 text-green-800 border-green-300',
    Icon: CheckCircleIcon,
  },
}

function getRiskColor(score) {
  if (score <= 30) return 'text-green-600'
  if (score <= 60) return 'text-yellow-600'
  return 'text-red-600'
}

function getRiskBarColor(score) {
  if (score <= 30) return 'bg-green-500'
  if (score <= 60) return 'bg-yellow-500'
  return 'bg-red-500'
}

function getRiskLabel(score) {
  if (score <= 30) return 'Low Risk'
  if (score <= 60) return 'Medium Risk'
  return 'High Risk'
}

function Skeleton() {
  return (
    <div className="space-y-4 animate-pulse">
      <div className="h-6 bg-gray-200 rounded w-1/3" />
      <div className="h-3 bg-gray-200 rounded w-full" />
      <div className="h-3 bg-gray-200 rounded w-5/6" />
      <div className="h-3 bg-gray-200 rounded w-4/6" />
      <div className="mt-4 h-4 bg-gray-200 rounded w-1/4" />
      <div className="space-y-2">
        <div className="h-3 bg-gray-200 rounded w-full" />
        <div className="h-3 bg-gray-200 rounded w-full" />
        <div className="h-3 bg-gray-200 rounded w-3/4" />
      </div>
    </div>
  )
}

export default function AIRecommendationPanel({ recommendation, isLoading, error }) {
  if (isLoading) {
    return (
      <div className="bg-white rounded-xl border border-gray-200 shadow-sm p-6">
        <div className="flex items-center gap-2 mb-4">
          <div className="w-5 h-5 bg-gray-200 rounded-full animate-pulse" />
          <div className="h-5 bg-gray-200 rounded w-40 animate-pulse" />
        </div>
        <Skeleton />
      </div>
    )
  }

  if (error) {
    return (
      <div className="bg-red-50 rounded-xl border border-red-200 p-6">
        <div className="flex items-start gap-3">
          <XCircleIcon className="w-5 h-5 text-red-500 flex-shrink-0 mt-0.5" />
          <div>
            <p className="text-sm font-semibold text-red-800">AI Analysis Unavailable</p>
            <p className="text-sm text-red-600 mt-1">{error}</p>
          </div>
        </div>
      </div>
    )
  }

  if (!recommendation) {
    return (
      <div className="bg-gray-50 rounded-xl border border-dashed border-gray-300 p-6 text-center">
        <p className="text-sm text-gray-500">No AI recommendation available yet.</p>
      </div>
    )
  }

  const rawAction = recommendation.recommended_action ?? recommendation.action
  const {
    confidence = 0,
    risk_score = 0,
    reasoning,
    suggested_steps = [],
    estimated_resolution_time,
    mode,
  } = recommendation

  const normalizedAction = rawAction?.toUpperCase()
  const actionInfo = actionConfig[normalizedAction] ?? {
    label: rawAction ?? 'Unknown',
    className: 'bg-gray-100 text-gray-700 border-gray-300',
    Icon: InformationCircleIcon,
  }
  const { label: actionLabel, className: actionClassName, Icon: ActionIcon } = actionInfo

  // Backend returns 0.0-1.0 float; normalize safely to 0-100%
  const rawConf = confidence <= 1 ? Math.round(confidence * 100) : Math.round(confidence)
  const confidencePct = Math.min(100, Math.max(0, rawConf))
  const riskPct = Math.min(100, Math.max(0, risk_score))

  return (
    <div className="bg-white rounded-xl border border-gray-200 shadow-sm p-6 space-y-5">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <span className="text-lg">🤖</span>
          <h3 className="text-base font-semibold text-gray-900">AI Recommendation</h3>
        </div>
        {mode && (
          <span className="px-2 py-0.5 rounded-full text-[11px] font-medium bg-gray-100 text-gray-600 border border-gray-200">
            {mode}
          </span>
        )}
      </div>

      <div className="flex items-center gap-3 flex-wrap">
        <span className="text-sm font-medium text-gray-500">Recommended Action:</span>
        <span
          className={clsx(
            'inline-flex items-center gap-1.5 px-3 py-1 rounded-full text-xs font-semibold border',
            actionClassName
          )}
        >
          <ActionIcon className="w-3.5 h-3.5" />
          {actionLabel}
        </span>
      </div>

      <div>
        <div className="flex items-center justify-between mb-1.5">
          <span className="text-sm font-medium text-gray-500">Confidence</span>
          <span className="text-sm font-bold text-gray-800">{confidencePct}%</span>
        </div>
        <div className="w-full bg-gray-200 rounded-full h-2.5">
          <div
            className="bg-[#003087] h-2.5 rounded-full transition-all duration-500"
            style={{ width: `${confidencePct}%` }}
          />
        </div>
      </div>

      <div>
        <div className="flex items-center justify-between mb-1.5">
          <span className="text-sm font-medium text-gray-500">Risk Score</span>
          <span className={clsx('text-sm font-bold', getRiskColor(riskPct))}>
            {riskPct} — {getRiskLabel(riskPct)}
          </span>
        </div>
        <div className="w-full bg-gray-200 rounded-full h-2.5">
          <div
            className={clsx('h-2.5 rounded-full transition-all duration-500', getRiskBarColor(riskPct))}
            style={{ width: `${riskPct}%` }}
          />
        </div>
      </div>

      {reasoning && (
        <div>
          <p className="text-sm font-medium text-gray-500 mb-1">Reasoning</p>
          <p className="text-sm text-gray-700 leading-relaxed bg-gray-50 rounded-lg p-3 border border-gray-100">
            {reasoning}
          </p>
        </div>
      )}

      {suggested_steps.length > 0 && (
        <div>
          <p className="text-sm font-medium text-gray-500 mb-2">Suggested Steps</p>
          <ol className="space-y-2">
            {suggested_steps.map((step, idx) => (
              <li key={idx} className="flex items-start gap-3 text-sm text-gray-700">
                <span className="flex-shrink-0 w-5 h-5 rounded-full bg-[#003087] text-white text-xs flex items-center justify-center font-semibold">
                  {idx + 1}
                </span>
                <span className="leading-relaxed">{step}</span>
              </li>
            ))}
          </ol>
        </div>
      )}

      {estimated_resolution_time && (
        <div className="flex items-center gap-2 pt-2 border-t border-gray-100">
          <span className="text-sm text-gray-500">Estimated Resolution:</span>
          <span className="text-sm font-semibold text-gray-800">{estimated_resolution_time}</span>
        </div>
      )}
    </div>
  )
}
