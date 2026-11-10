import React from 'react'
import clsx from 'clsx'

const priorityConfig = {
  CRITICAL: { label: 'Critical', className: 'bg-red-100 text-red-800 border-red-300' },
  HIGH: { label: 'High', className: 'bg-orange-100 text-orange-800 border-orange-300' },
  MEDIUM: { label: 'Medium', className: 'bg-yellow-100 text-yellow-800 border-yellow-300' },
  LOW: { label: 'Low', className: 'bg-gray-100 text-gray-600 border-gray-300' },
}

/**
 * PriorityBadge — colored pill for case/dispute priority levels.
 */
export default function PriorityBadge({ priority, className = '' }) {
  if (!priority) return null

  const normalized = priority.toUpperCase()
  const config = priorityConfig[normalized] ?? {
    label: priority,
    className: 'bg-gray-100 text-gray-600 border-gray-300',
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
