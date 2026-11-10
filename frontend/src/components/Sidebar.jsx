import React, { useEffect, useState } from 'react'
import { NavLink } from 'react-router-dom'
import {
  HomeIcon,
  ArrowsRightLeftIcon,
  ExclamationTriangleIcon,
  FolderOpenIcon,
} from '@heroicons/react/24/outline'
import {
  HomeIcon as HomeIconSolid,
  ArrowsRightLeftIcon as ArrowsRightLeftIconSolid,
  ExclamationTriangleIcon as ExclamationTriangleIconSolid,
  FolderOpenIcon as FolderOpenIconSolid,
} from '@heroicons/react/24/solid'
import clsx from 'clsx'
import { healthService } from '../services/api'

const primaryNavItems = [
  {
    label: 'Dashboard',
    to: '/',
    end: true,
    Icon: HomeIcon,
    IconActive: HomeIconSolid,
  },
  {
    label: 'Transactions',
    to: '/transactions',
    Icon: ArrowsRightLeftIcon,
    IconActive: ArrowsRightLeftIconSolid,
  },
]

const secondaryNavItems = [
  {
    label: 'Disputes',
    to: '/disputes',
    Icon: ExclamationTriangleIcon,
    IconActive: ExclamationTriangleIconSolid,
  },
  {
    label: 'Cases',
    to: '/cases',
    Icon: FolderOpenIcon,
    IconActive: FolderOpenIconSolid,
  },
]

function NavItem({ label, to, end, Icon, IconActive }) {
  return (
    <NavLink
      to={to}
      end={end}
      className={({ isActive }) =>
        clsx(
          'flex items-center gap-3 px-4 py-2.5 rounded-lg text-sm font-medium transition-all duration-150',
          isActive
            ? 'bg-[#009cde] text-white shadow-sm'
            : 'text-blue-100 hover:bg-[#002070] hover:text-white'
        )
      }
    >
      {({ isActive }) => (
        <>
          {isActive ? (
            <IconActive className="w-5 h-5 flex-shrink-0" />
          ) : (
            <Icon className="w-5 h-5 flex-shrink-0" />
          )}
          <span>{label}</span>
        </>
      )}
    </NavLink>
  )
}

export default function Sidebar() {
  const [services, setServices] = useState(null)

  useEffect(() => {
    healthService
      .get()
      .then((res) => setServices(res.data?.services ?? null))
      .catch(() => setServices(null))
  }, [])

  const isMock = services?.paypal === 'mock'

  return (
    <aside className="flex flex-col w-64 min-h-screen bg-[#003087] text-white shadow-xl flex-shrink-0">
      {/* Header / Brand */}
      <div className="flex items-center gap-3 px-6 py-5 border-b border-[#002070]">
        <div className="flex items-center justify-center w-9 h-9 rounded-lg bg-[#009cde] font-extrabold text-white text-sm select-none shadow">
          PP
        </div>
        <div className="leading-tight">
          <p className="font-bold text-white text-base tracking-tight">Resolution</p>
          <p className="text-[#009cde] text-xs font-semibold tracking-widest uppercase">
            Copilot
          </p>
        </div>
      </div>

      {/* Navigation */}
      <nav className="flex-1 px-3 py-4 space-y-5">
        <div>
          <p className="px-4 mb-1.5 text-[11px] font-semibold uppercase tracking-wider text-blue-300">
            Transaction Resolution
          </p>
          <div className="space-y-1">
            {primaryNavItems.map((item) => (
              <NavItem key={item.to} {...item} />
            ))}
          </div>
        </div>

        <div>
          <p className="px-4 mb-1.5 text-[11px] font-semibold uppercase tracking-wider text-blue-300">
            Disputes (Secondary)
          </p>
          <div className="space-y-1">
            {secondaryNavItems.map((item) => (
              <NavItem key={item.to} {...item} />
            ))}
          </div>
        </div>
      </nav>

      {/* Environment Indicator + Footer */}
      <div className="px-4 py-4 border-t border-[#002070] space-y-2">
        {services && (
          <div
            className={clsx(
              'rounded-lg px-3 py-2 text-xs border',
              isMock
                ? 'bg-amber-400/15 border-amber-300/30 text-amber-100'
                : 'bg-green-400/15 border-green-300/30 text-green-100'
            )}
          >
            <p className="font-semibold">
              PayPal: {services.paypal === 'mock' ? 'Mock Mode (Simulated)' : services.paypal}
            </p>
            <p className="text-[11px] opacity-90">
              AI: {services.ai === 'openai' ? 'GPT-4o-mini' : 'Heuristic Grounded'}
            </p>
          </div>
        )}
        <p className="text-[11px] text-blue-200 text-center select-none">
          PayPal Transaction Resolution Copilot
        </p>
      </div>
    </aside>
  )
}
