import React from 'react'
import clsx from 'clsx'
import { ArrowUpIcon, ArrowDownIcon } from '@heroicons/react/20/solid'

/**
 * StatsCard
 *
 * Props:
 *   title:   string
 *   value:   string | number
 *   change:  number  (positive = up, negative = down; optional)
 *   unit:    string  (optional suffix, e.g. "%" or "h")
 *   icon:    ReactNode (optional icon element to render top-right)
 *   loading: bool
 */
export default function StatsCard({ title, value, change, unit = '', icon, loading = false }) {
  const isPositive = change > 0
  const isNegative = change < 0
  const hasChange = change !== undefined && change !== null

  if (loading) {
    return (
      <div className="bg-white rounded-xl border border-gray-200 shadow-sm p-6 space-y-3 animate-pulse">
        <div className="flex items-start justify-between">
          <div className="h-4 bg-gray-200 rounded w-28" />
          <div className="w-9 h-9 bg-gray-200 rounded-lg" />
        </div>
        <div className="h-8 bg-gray-200 rounded w-20" />
        <div className="h-4 bg-gray-200 rounded w-24" />
      </div>
    )
  }

  return (
    <div className="bg-white rounded-xl border border-gray-200 shadow-sm p-6 hover:shadow-md transition-shadow duration-200">
      {/* Top row: title + icon */}
      <div className="flex items-start justify-between">
        <p className="text-sm font-medium text-gray-500 leading-tight">{title}</p>
        {icon && (
          <div className="flex-shrink-0 w-9 h-9 rounded-lg bg-[#003087]/10 flex items-center justify-center text-[#003087]">
            {icon}
          </div>
        )}
      </div>

      {/* Value */}
      <p className="mt-2 text-3xl font-bold text-gray-900 tracking-tight">
        {value}
        {unit && <span className="text-xl font-semibold text-gray-500 ml-0.5">{unit}</span>}
      </p>

      {/* Change indicator */}
      {hasChange && (
        <div className="mt-2 flex items-center gap-1">
          {isPositive && (
            <>
              <ArrowUpIcon className="w-4 h-4 text-green-500" />
              <span className="text-sm font-semibold text-green-600">+{change}%</span>
            </>
          )}
          {isNegative && (
            <>
              <ArrowDownIcon className="w-4 h-4 text-red-500" />
              <span className="text-sm font-semibold text-red-600">{change}%</span>
            </>
          )}
          {!isPositive && !isNegative && (
            <span className="text-sm font-medium text-gray-500">No change</span>
          )}
          <span className="text-xs text-gray-400 ml-1">vs last month</span>
        </div>
      )}
    </div>
  )
}
