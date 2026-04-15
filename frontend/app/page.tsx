import Link from "next/link";
import CompanySearch from "@/components/CompanySearch";

const API_BASE = "http://localhost:8001";

type Company = {
  id: number;
  ticker: string;
  name: string;
  sector: string | null;
  industry: string | null;
};

export default async function Home() {
  let companies: Company[] = [];
  try {
    const res = await fetch(`${API_BASE}/api/companies`, { cache: "no-store" });
    if (res.ok) companies = await res.json();
  } catch {
    // backend may not be running during build
  }

  return (
    <main className="min-h-screen bg-gray-50 flex flex-col">
      {/* Header */}
      <header className="bg-gradient-to-r from-slate-900 to-slate-700 text-white py-16 px-6">
        <div className="max-w-6xl mx-auto">
          <div className="flex items-start justify-between">
            <div>
              <h1 className="text-4xl font-bold tracking-tight mb-2">CheckIt Analytics</h1>
              <p className="text-slate-400 text-lg font-light">Earnings Intelligence Platform</p>
            </div>
            <Link
              href="/evaluation"
              className="text-xs font-semibold text-slate-400 hover:text-white border border-slate-600 hover:border-slate-400 px-3 py-1.5 rounded-lg transition-colors mt-1"
            >
              Evaluation Dashboard →
            </Link>
          </div>
        </div>
      </header>

      {/* Search + Cards */}
      <div className="flex-1 flex flex-col items-center justify-start pt-12 pb-16 px-6">
        {companies.length === 0 ? (
          <p className="text-gray-400 text-sm">
            No companies found — make sure the backend is running on port 8001.
          </p>
        ) : (
          <CompanySearch
            companies={companies.map((c) => ({
              ticker: c.ticker,
              name: c.name,
              sector: c.sector,
            }))}
          />
        )}
      </div>

      <footer className="px-6 py-5 border-t border-gray-100 text-xs text-gray-400 text-center">
        Data sourced from SEC EDGAR and company-provided transcripts.&nbsp;&nbsp;·&nbsp;&nbsp;Not financial advice.
      </footer>
    </main>
  );
}
