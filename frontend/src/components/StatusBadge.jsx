import React from 'react'
import clsx from 'clsx'

const statusConfig = {
  COMPLETED: { label: 'Completed', className: 'bg-green-100 text-green-800 border-green-200' },
  RESOLVED: { label: 'Resolved', className: 'bg-green-100 text-green-800 border-green-200' },
  PENDING: { label: 'Pending', className: 'bg-amber-100 text-amber-800 border-amber-200' },
  OPEN: { label: 'Open', className: 'bg-amber-100 text-amber-800 border-amber-200' },
  UNDER_REVIEW: { label: 'Under Review', className: 'bg-yellow-100 text-yellow-800 border-yellow-200' },
  FAILED: { label: 'Failed', className: 'bg-red-100 text-red-800 border-red-200' },
  CLOSED: { label: 'Closed', className: 'bg-red-100 text-red-800 border-red-200' },
  DISPUTED: { label: 'Disputed', className: 'bg-orange-100 text-orange-800 border-orange-200' },
  REFUNDED: { label: 'Refunded', className: 'bg-blue-100 text-blue-800 border-blue-200' },
  CANCELLED: { label: 'Cancelled', className: 'bg-gray-100 text-gray-700 border-gray-200' },
  IN_PROGRESS: { label: 'In Progress', className: 'bg-sky-100 text-sky-800 border-sky-200' },
  ESCALATED: { label: 'Escalated', className: 'bg-purple-100 text-purple-800 border-purple-200' },
}

/**
 * StatusBadge — renders a colored pill for a given status string.
 * Falls back gracefully for unknown statuses.
 */
export default function StatusBadge({ status, className = '' }) {
  if (!status) return null

  const normalized = status.toUpperCase().replace(/[\s-]/g, '_')
  const config = statusConfig[normalized] ?? {
    label: status,
    className: 'bg-gray-100 text-gray-700 border-gray-200',
  }

  return (
    <span
      className={clsx(
        'inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-semibold border',
        config.className,
        className
      )}
    >
      {config.label}
    </span>
  )
}
