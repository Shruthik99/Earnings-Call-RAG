import Link from "next/link";

interface Props {
  ticker: string;
  name: string;
  sector: string | null;
  industry: string | null;
}

const SECTOR_COLORS: Record<string, string> = {
  Technology:          "bg-blue-100 text-blue-700",
  "Consumer Cyclical": "bg-orange-100 text-orange-700",
  "Financial Services":"bg-emerald-100 text-emerald-700",
  Healthcare:          "bg-pink-100 text-pink-700",
  "Consumer Defensive":"bg-lime-100 text-lime-700",
  Industrials:         "bg-purple-100 text-purple-700",
};

export default function CompanyCard({ ticker, name, sector, industry }: Props) {
  const sectorClass =
    sector && SECTOR_COLORS[sector]
      ? SECTOR_COLORS[sector]
      : "bg-gray-100 text-gray-600";

  return (
    <Link href={`/company/${ticker}`} className="group block h-full">
      <div className="h-full bg-white rounded-xl border border-gray-200 p-5 transition-all duration-150 group-hover:shadow-md group-hover:border-blue-300">
        <div className="flex items-start justify-between mb-3">
          <span className="text-2xl font-bold font-mono text-blue-700">{ticker}</span>
          {sector && (
            <span className={`text-[11px] font-semibold px-2 py-0.5 rounded-full ${sectorClass}`}>
              {sector}
            </span>
          )}
        </div>
        <p className="text-sm font-medium text-gray-800 leading-snug">{name}</p>
        {industry && (
          <p className="text-xs text-gray-400 mt-1 truncate">{industry}</p>
        )}
      </div>
    </Link>
  );
}
