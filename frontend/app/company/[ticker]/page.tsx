import { notFound } from "next/navigation";
import Link from "next/link";
import CompanyDetail, { type CompanyData } from "@/components/CompanyDetail";

const API_BASE = "http://localhost:8001";

const SECTOR_COLORS: Record<string, string> = {
  Technology:           "bg-blue-100 text-blue-700",
  "Consumer Cyclical":  "bg-orange-100 text-orange-700",
  "Financial Services": "bg-emerald-100 text-emerald-700",
  Healthcare:           "bg-pink-100 text-pink-700",
};

export default async function Page({
  params,
}: {
  params: Promise<{ ticker: string }>;
}) {
  const { ticker } = await params;

  const res = await fetch(
    `${API_BASE}/api/companies/${ticker.toUpperCase()}`,
    { cache: "no-store" }
  );

  if (!res.ok) notFound();

  const company: CompanyData = await res.json();

  const sectorClass =
    company.sector && SECTOR_COLORS[company.sector]
      ? SECTOR_COLORS[company.sector]
      : "bg-gray-100 text-gray-600";

  return (
    <main className="min-h-screen bg-gray-50">
      {/* Nav bar */}
      <header className="bg-slate-900 text-white py-4 px-6">
        <div className="max-w-5xl mx-auto flex items-center gap-3 text-sm">
          <Link
            href="/"
            className="text-slate-400 hover:text-white transition-colors"
          >
            ← Companies
          </Link>
          <span className="text-slate-700">|</span>
          <span className="font-mono font-semibold text-blue-400">
            {company.ticker}
          </span>
        </div>
      </header>

      <div className="max-w-5xl mx-auto px-6 py-8">
        {/* Company header */}
        <div className="mb-8">
          <div className="flex flex-wrap items-baseline gap-3 mb-2">
            <h1 className="text-2xl font-bold text-gray-900">{company.name}</h1>
            <span className="text-xl font-mono font-bold text-blue-600">
              {company.ticker}
            </span>
          </div>
          <div className="flex flex-wrap gap-2">
            {company.sector && (
              <span className={`text-xs font-semibold px-2.5 py-1 rounded-full ${sectorClass}`}>
                {company.sector}
              </span>
            )}
            {company.industry && (
              <span className="text-xs font-semibold px-2.5 py-1 rounded-full bg-gray-100 text-gray-600">
                {company.industry}
              </span>
            )}
          </div>
        </div>

        {/* Interactive detail (quarter selector + financials + chat) */}
        <CompanyDetail company={company} />
      </div>
    </main>
  );
}
