"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";

interface Company {
  ticker: string;
  name: string;
  sector: string | null;
}

const SECTOR_COLORS: Record<string, string> = {
  Technology:           "bg-blue-100 text-blue-700",
  "Consumer Cyclical":  "bg-orange-100 text-orange-700",
  "Financial Services": "bg-emerald-100 text-emerald-700",
  Healthcare:           "bg-pink-100 text-pink-700",
};

export default function CompanySearch({ companies }: { companies: Company[] }) {
  const router = useRouter();
  const [query, setQuery] = useState("");

  const filtered =
    query.trim() === ""
      ? companies
      : companies.filter(
          (c) =>
            c.ticker.toLowerCase().includes(query.toLowerCase()) ||
            c.name.toLowerCase().includes(query.toLowerCase()) ||
            (c.sector ?? "").toLowerCase().includes(query.toLowerCase())
        );

  return (
    <div className="w-full max-w-3xl mx-auto">
      {/* Search input */}
      <div className="relative mb-10">
        <span className="absolute left-4 top-1/2 -translate-y-1/2 text-gray-400 pointer-events-none">
          <svg width="20" height="20" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
            <circle cx="11" cy="11" r="8" /><path d="m21 21-4.35-4.35" />
          </svg>
        </span>
        <input
          type="text"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search by company or ticker..."
          className="w-full pl-12 pr-5 py-4 rounded-2xl border border-gray-200 bg-white text-gray-900 placeholder-gray-400 text-base shadow-md focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent transition-shadow duration-200"
        />
      </div>

      {/* Company cards grid */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
        {filtered.map((c) => {
          const sectorClass =
            c.sector && SECTOR_COLORS[c.sector]
              ? SECTOR_COLORS[c.sector]
              : "bg-gray-100 text-gray-600";
          return (
            <button
              key={c.ticker}
              onClick={() => router.push(`/company/${c.ticker}`)}
              className="group text-left bg-white border border-gray-200 rounded-2xl p-5 shadow-sm hover:shadow-md hover:border-blue-200 transition-all duration-200 cursor-pointer"
            >
              <div className="flex items-start justify-between mb-2">
                <span className="font-mono font-bold text-2xl text-blue-600 leading-none tracking-tight">
                  {c.ticker}
                </span>
                <svg
                  className="w-4 h-4 text-gray-300 group-hover:text-blue-400 transition-colors duration-200 mt-1 shrink-0"
                  fill="none" stroke="currentColor" strokeWidth="2.5" viewBox="0 0 24 24"
                >
                  <path d="M9 18l6-6-6-6" />
                </svg>
              </div>
              <p className="text-sm text-gray-700 font-medium mb-3 leading-snug">{c.name}</p>
              {c.sector && (
                <span className={`text-[11px] font-semibold px-2.5 py-1 rounded-full ${sectorClass}`}>
                  {c.sector}
                </span>
              )}
            </button>
          );
        })}
      </div>
    </div>
  );
}
