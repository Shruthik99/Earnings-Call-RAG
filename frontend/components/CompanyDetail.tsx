"use client";

import { useState, useEffect } from "react";
import ChatInterface from "@/components/ChatInterface";

const API_BASE = "http://localhost:8001";

// ── Types ────────────────────────────────────────────────────────────────────

export interface EarningsCall {
  id: number;
  fiscal_year: number;
  fiscal_quarter: string;
  call_date: string | null;
  status: string;
  transcript_source: string | null;
}

export interface CompanyData {
  id: number;
  ticker: string;
  name: string;
  sector: string | null;
  industry: string | null;
  earnings_calls: EarningsCall[];
}

interface FinancialMetrics {
  revenue_actual: number | null;
  revenue_consensus: number | null;
  eps_actual: number | null;
  eps_consensus: number | null;
  revenue_yoy_growth: number | null;
  net_income: number | null;
  guidance_revenue_low: number | null;
  guidance_revenue_high: number | null;
  guidance_eps_low: number | null;
  guidance_eps_high: number | null;
  source: string | null;
}

// ── Helpers ──────────────────────────────────────────────────────────────────

function fmtRevenue(v: number | null): string {
  if (v === null) return "—";
  return v >= 1000 ? `$${(v / 1000).toFixed(2)}B` : `$${v.toFixed(0)}M`;
}

function fmtEPS(v: number | null): string {
  if (v === null) return "—";
  return `$${v.toFixed(2)}`;
}

function fmtGrowth(v: number | null): string {
  if (v === null) return "—";
  const sign = v > 0 ? "+" : "";
  return `${sign}${v.toFixed(1)}%`;
}

// ── Financial snapshot card ───────────────────────────────────────────────────

function MetricCard({
  label,
  value,
  sub,
  accentClass,
}: {
  label: string;
  value: string;
  sub?: string;
  accentClass: string;
}) {
  return (
    <div className={`bg-white rounded-xl border border-gray-200 border-t-4 ${accentClass} p-4 shadow-sm`}>
      <p className="text-xs text-gray-400 font-medium uppercase tracking-wide mb-1">{label}</p>
      <p className="text-xl font-bold text-gray-900">{value}</p>
      {sub && <p className="text-xs text-gray-400 mt-0.5">{sub}</p>}
    </div>
  );
}

function FinancialSnapshot({
  data,
  loading,
}: {
  data: FinancialMetrics | null;
  loading: boolean;
}) {
  if (loading) {
    return (
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-8 animate-pulse">
        {[...Array(4)].map((_, i) => (
          <div key={i} className="h-20 bg-gray-100 rounded-xl" />
        ))}
      </div>
    );
  }

  if (!data || Object.values(data).every((v) => v === null || typeof v === "string")) {
    return null;
  }

  // EPS guard: hide if > $100 (data error)
  const showEps = data.eps_actual !== null && Math.abs(data.eps_actual) <= 100;
  const hasGuidance =
    data.guidance_revenue_low !== null || data.guidance_revenue_high !== null;
  const guidance = hasGuidance
    ? `${fmtRevenue(data.guidance_revenue_low)} – ${fmtRevenue(data.guidance_revenue_high)}`
    : null;

  const cards: React.ReactNode[] = [];

  if (data.revenue_actual !== null) {
    cards.push(
      <MetricCard
        key="rev"
        label="Revenue"
        value={fmtRevenue(data.revenue_actual)}
        sub={data.revenue_consensus ? `est. ${fmtRevenue(data.revenue_consensus)}` : undefined}
        accentClass="border-t-green-400"
      />
    );
  }

  if (showEps) {
    cards.push(
      <MetricCard
        key="eps"
        label="EPS"
        value={fmtEPS(data.eps_actual)}
        sub={data.eps_consensus ? `est. ${fmtEPS(data.eps_consensus)}` : undefined}
        accentClass="border-t-blue-400"
      />
    );
  }

  if (data.revenue_yoy_growth !== null) {
    cards.push(
      <MetricCard
        key="yoy"
        label="YoY Growth"
        value={fmtGrowth(data.revenue_yoy_growth)}
        accentClass="border-t-orange-400"
      />
    );
  }

  if (guidance) {
    cards.push(
      <MetricCard
        key="guidance"
        label="FY Guidance (Rev)"
        value={guidance}
        accentClass="border-t-purple-400"
      />
    );
  }

  if (cards.length === 0) return null;

  return (
    <div className={`grid grid-cols-2 sm:grid-cols-${Math.min(cards.length, 4)} gap-3 mb-8`}>
      {cards}
    </div>
  );
}

// ── Main component ────────────────────────────────────────────────────────────

export default function CompanyDetail({ company }: { company: CompanyData }) {
  // Only show transcript-based quarters (Q1–Q4) in the selector.
  // 8-K, 10-K, 10-Q calls are supporting data only and not user-selectable.
  const transcriptCalls = company.earnings_calls.filter((c) =>
    /^Q[1-4]$/i.test(c.fiscal_quarter)
  );

  const sortedCalls = [...transcriptCalls].sort((a, b) => {
    if (b.fiscal_year !== a.fiscal_year) return b.fiscal_year - a.fiscal_year;
    return b.fiscal_quarter.localeCompare(a.fiscal_quarter);
  });

  const [selectedIdx, setSelectedIdx] = useState(0);
  const [financials, setFinancials] = useState<FinancialMetrics | null>(null);
  const [loadingFin, setLoadingFin] = useState(false);

  const selected = sortedCalls[selectedIdx];

  useEffect(() => {
    if (!selected) return;
    setFinancials(null);
    setLoadingFin(true);

    fetch(
      `${API_BASE}/api/earnings/${company.ticker}/${selected.fiscal_quarter}/${selected.fiscal_year}`
    )
      .then((r) => r.json())
      .then((data) => {
        setFinancials(data.financial_metrics ?? null);
        setLoadingFin(false);
      })
      .catch(() => setLoadingFin(false));
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedIdx]);

  if (sortedCalls.length === 0) {
    return (
      <p className="text-gray-400 text-sm">No earnings calls available for this company.</p>
    );
  }

  return (
    <div className="space-y-8">
      {/* Quarter selector */}
      <div className="flex items-center gap-3">
        <select
          id="quarter-select"
          value={selectedIdx}
          onChange={(e) => setSelectedIdx(Number(e.target.value))}
          className="border border-gray-200 rounded-xl px-4 py-2.5 text-sm bg-white text-gray-800 font-medium focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent shadow-sm transition-shadow"
        >
          {sortedCalls.map((call, i) => (
            <option key={call.id} value={i}>
              {call.fiscal_quarter} FY{call.fiscal_year}
              {call.call_date ? ` — ${call.call_date}` : ""}
            </option>
          ))}
        </select>
      </div>

      {/* Financial snapshot */}
      <FinancialSnapshot data={financials} loading={loadingFin} />

      {/* Chat interface */}
      {selected && (
        <div className="bg-white rounded-2xl border border-gray-200 p-6 shadow-sm">
          <ChatInterface
            ticker={company.ticker}
            quarter={selected.fiscal_quarter}
            year={selected.fiscal_year}
          />
        </div>
      )}
    </div>
  );
}
