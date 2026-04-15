import Link from "next/link";

const API_BASE = "http://localhost:8001";

// ── Types ────────────────────────────────────────────────────────────────────

interface MetricInfo {
  avg: number | null;
  target: number;
  pass: boolean | null;
  higher_is_better: boolean;
}

interface Summary {
  total_questions: number;
  composite_score: number | null;
  metrics: Record<string, MetricInfo>;
  by_category: Record<string, { composite: number | null; count: number }>;
  last_run: string | null;
}

interface EvalRow {
  id: number;
  ticker: string;
  question: string;
  category: string;
  grounding_score: number | null;
  hallucination_rate: number | null;
  reasoning_score: number | null;
  completeness_score: number | null;
  citation_rate: number | null;
  composite_score: number | null;
  consistency: string | null;
}

// ── Helpers ──────────────────────────────────────────────────────────────────

function pct(v: number | null): string {
  if (v === null) return "—";
  return `${(v * 100).toFixed(1)}%`;
}

function scoreColor(v: number | null, higherIsBetter = true): string {
  if (v === null) return "text-gray-400";
  const ok = higherIsBetter ? v >= 0.75 : v <= 0.25;
  const warn = higherIsBetter ? v >= 0.5 : v <= 0.5;
  if (ok) return "text-emerald-600";
  if (warn) return "text-amber-500";
  return "text-red-500";
}

function compositeColor(v: number | null): string {
  if (v === null) return "text-gray-400";
  if (v >= 0.75) return "text-emerald-500";
  if (v >= 0.5)  return "text-amber-500";
  return "text-red-500";
}

function compositeBg(v: number | null): string {
  if (v === null) return "bg-gray-50 border-gray-200";
  if (v >= 0.75) return "bg-emerald-50 border-emerald-200";
  if (v >= 0.5)  return "bg-amber-50 border-amber-200";
  return "bg-red-50 border-red-200";
}

function categoryBadgeClass(cat: string): string {
  const m: Record<string, string> = {
    earnings:  "bg-blue-100 text-blue-700",
    financial: "bg-green-100 text-green-700",
    risk:      "bg-amber-100 text-amber-700",
    edge_case: "bg-purple-100 text-purple-700",
    other:     "bg-gray-100 text-gray-600",
  };
  return m[cat] ?? "bg-gray-100 text-gray-600";
}

function consistencyBadge(v: string | null): string {
  if (!v) return "bg-gray-100 text-gray-500";
  const m: Record<string, string> = {
    aligned:  "bg-emerald-100 text-emerald-700",
    mixed:    "bg-amber-100 text-amber-700",
    conflict: "bg-red-100 text-red-700",
    "n/a":    "bg-gray-100 text-gray-500",
  };
  return m[v.toLowerCase()] ?? "bg-gray-100 text-gray-500";
}

const METRIC_LABELS: Record<string, { label: string; description: string }> = {
  grounding:        { label: "Grounding",         description: "Evidence with quoted text" },
  hallucination_rate: { label: "Hallucination Rate", description: "Low confidence proxy" },
  reasoning_score:  { label: "Reasoning",          description: "Structured field coverage (÷5)" },
  completeness:     { label: "Completeness",        description: "Key points density" },
  citation_rate:    { label: "Citation Rate",       description: "Responses with citations" },
};

const CATEGORY_LABELS: Record<string, string> = {
  earnings:  "Earnings",
  financial: "Financial",
  risk:      "Risk",
  edge_case: "Edge Cases",
  other:     "Other",
};

// ── Page ─────────────────────────────────────────────────────────────────────

export default async function EvaluationPage() {
  let summary: Summary | null = null;
  let results: EvalRow[] = [];

  try {
    [summary, results] = await Promise.all([
      fetch(`${API_BASE}/api/evaluation/summary`, { cache: "no-store" }).then((r) => r.json()),
      fetch(`${API_BASE}/api/evaluation`, { cache: "no-store" }).then((r) => r.json()),
    ]);
  } catch {
    // backend may be down
  }

  const composite = summary?.composite_score ?? null;
  const lastRun = summary?.last_run
    ? new Date(summary.last_run).toLocaleString("en-US", {
        month: "short", day: "numeric", year: "numeric",
        hour: "2-digit", minute: "2-digit",
      })
    : null;

  return (
    <main className="min-h-screen bg-gray-50">
      {/* Header */}
      <header className="bg-gradient-to-r from-slate-900 to-slate-700 text-white py-10 px-6">
        <div className="max-w-6xl mx-auto">
          <div className="flex items-center gap-3 mb-1">
            <Link href="/" className="text-slate-400 hover:text-white text-sm transition-colors">
              ← CheckIt Analytics
            </Link>
          </div>
          <h1 className="text-3xl font-bold tracking-tight mb-1">Evaluation Dashboard</h1>
          <p className="text-slate-400 text-sm font-light">
            RAG pipeline quality metrics across {summary?.total_questions ?? "—"} test questions
            {lastRun && <span className="ml-3 text-slate-500">Last run: {lastRun}</span>}
          </p>
        </div>
      </header>

      <div className="max-w-6xl mx-auto px-6 py-10 space-y-10">

        {/* ── Composite Score Hero ─────────────────────────────── */}
        <section className={`rounded-2xl border-2 p-8 flex flex-col sm:flex-row items-center gap-6 ${compositeBg(composite)}`}>
          <div className="text-center sm:text-left">
            <p className="text-xs font-semibold uppercase tracking-widest text-gray-400 mb-1">
              Overall Composite Score
            </p>
            <p className={`text-8xl font-black tracking-tight leading-none ${compositeColor(composite)}`}>
              {composite !== null ? (composite * 100).toFixed(1) : "—"}
              <span className="text-4xl font-bold">%</span>
            </p>
          </div>
          <div className="flex-1 sm:border-l sm:border-gray-200 sm:pl-8 space-y-1 text-sm text-gray-600">
            <p>
              <span className="font-semibold">{summary?.total_questions ?? 0}</span> questions evaluated
            </p>
            <p>
              <span className="font-semibold text-emerald-600">
                {results.filter((r) => (r.composite_score ?? 0) >= 0.75).length}
              </span>{" "}
              passing (≥ 75%)
            </p>
            <p>
              <span className="font-semibold text-red-500">
                {results.filter((r) => r.composite_score !== null && r.composite_score < 0.5).length}
              </span>{" "}
              failing (&lt; 50%)
            </p>
            <p className="pt-2 text-xs text-gray-400 font-mono">
              Composite = 0.30×G + 0.20×(R/5) + 0.15×C + 0.15×K + 0.10×Cit + 0.10×(1−H)
            </p>
          </div>
        </section>

        {/* ── Metric Cards ─────────────────────────────────────── */}
        {summary && (
          <section>
            <h2 className="text-xs font-semibold uppercase tracking-widest text-gray-400 mb-4">
              Individual Metrics
            </h2>
            <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-4">
              {Object.entries(summary.metrics).map(([key, m]) => {
                const meta = METRIC_LABELS[key];
                const displayVal = key === "hallucination_rate"
                  ? m.avg !== null ? `${(m.avg * 100).toFixed(1)}%` : "—"
                  : pct(m.avg);
                const targetLabel = key === "hallucination_rate"
                  ? `≤ ${pct(m.target)}`
                  : `≥ ${pct(m.target)}`;
                return (
                  <div key={key} className="bg-white rounded-xl border border-gray-200 p-4 shadow-sm">
                    <div className="flex items-start justify-between mb-3">
                      <p className="text-xs font-semibold text-gray-500 leading-snug">
                        {meta?.label ?? key}
                      </p>
                      <span
                        className={`text-[10px] font-bold px-1.5 py-0.5 rounded-full ${
                          m.pass ? "bg-emerald-100 text-emerald-700" : "bg-red-100 text-red-600"
                        }`}
                      >
                        {m.pass ? "PASS" : "FAIL"}
                      </span>
                    </div>
                    <p className={`text-2xl font-bold mb-1 ${scoreColor(m.avg, m.higher_is_better)}`}>
                      {displayVal}
                    </p>
                    <p className="text-[10px] text-gray-400">
                      Target: {targetLabel}
                    </p>
                    <p className="text-[10px] text-gray-300 mt-1 leading-snug">
                      {meta?.description}
                    </p>
                    {/* Mini bar */}
                    <div className="mt-3 h-1.5 bg-gray-100 rounded-full overflow-hidden">
                      <div
                        className={`h-full rounded-full ${m.pass ? "bg-emerald-400" : "bg-red-400"}`}
                        style={{ width: `${Math.min(100, ((m.avg ?? 0) / (key === "hallucination_rate" ? 1 : 1)) * 100)}%` }}
                      />
                    </div>
                  </div>
                );
              })}
            </div>
          </section>
        )}

        {/* ── Category Breakdown ───────────────────────────────── */}
        {summary && Object.keys(summary.by_category).length > 0 && (
          <section>
            <h2 className="text-xs font-semibold uppercase tracking-widest text-gray-400 mb-4">
              Scores by Category
            </h2>
            <div className="bg-white rounded-xl border border-gray-200 p-6 shadow-sm space-y-5">
              {Object.entries(summary.by_category)
                .sort((a, b) => (b[1].composite ?? 0) - (a[1].composite ?? 0))
                .map(([cat, data]) => {
                  const comp = data.composite ?? 0;
                  const barPct = Math.round(comp * 100);
                  const barColor =
                    comp >= 0.75 ? "bg-emerald-400"
                    : comp >= 0.5 ? "bg-amber-400"
                    : "bg-red-400";
                  return (
                    <div key={cat} className="flex items-center gap-4">
                      <div className="w-28 shrink-0">
                        <span className={`text-[11px] font-semibold px-2 py-0.5 rounded-full ${categoryBadgeClass(cat)}`}>
                          {CATEGORY_LABELS[cat] ?? cat}
                        </span>
                      </div>
                      <div className="flex-1 h-6 bg-gray-100 rounded-full overflow-hidden">
                        <div
                          className={`h-full rounded-full flex items-center justify-end pr-2 ${barColor}`}
                          style={{ width: `${barPct}%` }}
                        >
                          <span className="text-[10px] font-bold text-white">{barPct}%</span>
                        </div>
                      </div>
                      <div className="w-20 shrink-0 text-right">
                        <span className="text-xs text-gray-400">{data.count} questions</span>
                      </div>
                    </div>
                  );
                })}
            </div>
          </section>
        )}

        {/* ── Questions Table ──────────────────────────────────── */}
        <section>
          <h2 className="text-xs font-semibold uppercase tracking-widest text-gray-400 mb-4">
            All Questions — sorted by composite score
          </h2>
          <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-gray-100 bg-gray-50">
                  <th className="text-left px-4 py-3 text-[10px] font-semibold uppercase tracking-widest text-gray-400 w-16">
                    Ticker
                  </th>
                  <th className="text-left px-4 py-3 text-[10px] font-semibold uppercase tracking-widest text-gray-400">
                    Question
                  </th>
                  <th className="text-center px-3 py-3 text-[10px] font-semibold uppercase tracking-widest text-gray-400 w-20 hidden md:table-cell">
                    Ground.
                  </th>
                  <th className="text-center px-3 py-3 text-[10px] font-semibold uppercase tracking-widest text-gray-400 w-20 hidden lg:table-cell">
                    Halluci.
                  </th>
                  <th className="text-center px-3 py-3 text-[10px] font-semibold uppercase tracking-widest text-gray-400 w-20 hidden lg:table-cell">
                    Reason.
                  </th>
                  <th className="text-center px-3 py-3 text-[10px] font-semibold uppercase tracking-widest text-gray-400 w-20 hidden md:table-cell">
                    Consist.
                  </th>
                  <th className="text-center px-3 py-3 text-[10px] font-semibold uppercase tracking-widest text-gray-400 w-24">
                    Composite
                  </th>
                </tr>
              </thead>
              <tbody>
                {results.map((row, i) => (
                  <tr
                    key={row.id}
                    className={`border-b border-gray-50 hover:bg-gray-50 transition-colors ${
                      i % 2 === 0 ? "" : "bg-gray-50/40"
                    }`}
                  >
                    <td className="px-4 py-3">
                      <div className="flex flex-col gap-1">
                        <span className="font-mono font-bold text-blue-600 text-xs">
                          {row.ticker}
                        </span>
                        <span className={`text-[10px] font-semibold px-1.5 py-0.5 rounded-full w-fit ${categoryBadgeClass(row.category)}`}>
                          {row.category}
                        </span>
                      </div>
                    </td>
                    <td className="px-4 py-3 text-gray-700 text-xs leading-relaxed max-w-xs">
                      {row.question}
                    </td>
                    <td className={`px-3 py-3 text-center text-xs font-semibold hidden md:table-cell ${scoreColor(row.grounding_score)}`}>
                      {pct(row.grounding_score)}
                    </td>
                    <td className={`px-3 py-3 text-center text-xs font-semibold hidden lg:table-cell ${scoreColor(row.hallucination_rate, false)}`}>
                      {pct(row.hallucination_rate)}
                    </td>
                    <td className={`px-3 py-3 text-center text-xs font-semibold hidden lg:table-cell ${scoreColor(row.reasoning_score !== null ? row.reasoning_score / 5 : null)}`}>
                      {row.reasoning_score !== null ? `${row.reasoning_score}/5` : "—"}
                    </td>
                    <td className="px-3 py-3 text-center hidden md:table-cell">
                      {row.consistency ? (
                        <span className={`text-[10px] font-semibold px-2 py-0.5 rounded-full ${consistencyBadge(row.consistency)}`}>
                          {row.consistency}
                        </span>
                      ) : (
                        <span className="text-gray-300">—</span>
                      )}
                    </td>
                    <td className="px-3 py-3 text-center">
                      <span className={`text-sm font-bold ${compositeColor(row.composite_score)}`}>
                        {row.composite_score !== null
                          ? `${(row.composite_score * 100).toFixed(0)}%`
                          : "—"}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {results.length === 0 && (
              <p className="text-sm text-gray-400 text-center py-12">
                No evaluation results found. Run the eval suite first.
              </p>
            )}
          </div>
        </section>

        {/* ── Formula ─────────────────────────────────────────── */}
        <section className="bg-slate-900 rounded-xl px-6 py-5">
          <p className="text-[10px] font-semibold uppercase tracking-widest text-slate-500 mb-2">
            Scoring Formula
          </p>
          <p className="font-mono text-sm text-slate-300">
            Composite = 0.30×<span className="text-emerald-400">G</span>{" "}
            + 0.20×(<span className="text-blue-400">R</span>/5){" "}
            + 0.15×<span className="text-purple-400">C</span>{" "}
            + 0.15×<span className="text-orange-400">K</span>{" "}
            + 0.10×<span className="text-pink-400">Cit</span>{" "}
            + 0.10×(1−<span className="text-red-400">H</span>)
          </p>
          <div className="mt-3 grid grid-cols-2 sm:grid-cols-3 gap-x-8 gap-y-1 text-[11px] text-slate-500">
            <span><span className="text-emerald-400 font-mono font-bold">G</span> = Grounding score (evidence quality)</span>
            <span><span className="text-blue-400 font-mono font-bold">R</span> = Reasoning score (1–5 field coverage)</span>
            <span><span className="text-purple-400 font-mono font-bold">C</span> = Completeness (key points)</span>
            <span><span className="text-orange-400 font-mono font-bold">K</span> = Consistency alignment</span>
            <span><span className="text-pink-400 font-mono font-bold">Cit</span> = Citation rate</span>
            <span><span className="text-red-400 font-mono font-bold">H</span> = Hallucination rate (lower → higher score)</span>
          </div>
        </section>

        {/* ── Score Transparency ───────────────────────────────── */}
        <section className="border-t border-gray-200 pt-10">
          <h2 className="text-xs font-semibold uppercase tracking-widest text-gray-400 mb-6">
            Scoring Methodology &amp; Limitations
          </h2>

          {/* Metric breakdowns */}
          <div className="space-y-0 divide-y divide-gray-100">
            {[
              {
                metric: "Grounding",
                score: "1.000",
                what: "Measures whether evidence citations exist in the response. Current heuristic checks for presence of evidence array.",
                limit: "Does not verify if cited evidence actually supports the claim.",
                future: "Add semantic similarity check between claim and cited chunk.",
              },
              {
                metric: "Citation",
                score: "1.000",
                what: "Measures whether quote fields are populated in evidence items.",
                limit: "Does not verify exact quote match against source transcript.",
                future: "Add string-match verification between quoted text and original chunk content.",
              },
              {
                metric: "Completeness",
                score: "1.000",
                what: "Measures whether key_points array has 3+ items.",
                limit: "Does not verify if all important topics from the source are covered.",
                future: "Define expected answer points per question and measure coverage.",
              },
              {
                metric: "Hallucination",
                score: "0.070",
                what: "Estimated from confidence level. Low-confidence responses are flagged as potential hallucination.",
                limit: "Proxy metric, not true hallucination detection.",
                future: "Use a separate LLM-as-judge to verify each claim against retrieved chunks.",
              },
              {
                metric: "Reasoning",
                score: "5.0 / 5",
                what: "Measures structural completeness of JSON response — all fields present, proper types.",
                limit: "Does not evaluate analytical depth or insight quality.",
                future: "Human evaluation on 1–5 scale for analytical quality.",
              },
            ].map(({ metric, score, what, limit, future }) => (
              <div key={metric} className="py-5 grid grid-cols-1 sm:grid-cols-[140px_1fr] gap-2 sm:gap-6">
                <div className="shrink-0">
                  <span className="text-sm font-semibold text-gray-600">{metric}</span>
                  <span className="ml-2 text-sm font-mono text-gray-400">({score})</span>
                </div>
                <div className="space-y-1 text-xs text-gray-500 leading-relaxed">
                  <p>{what}</p>
                  <p>
                    <span className="font-semibold text-gray-400">Limitation: </span>
                    {limit}
                  </p>
                  <p>
                    <span className="font-semibold text-gray-400">Future improvement: </span>
                    {future}
                  </p>
                </div>
              </div>
            ))}
          </div>

          {/* Next steps */}
          <div className="mt-8 bg-gray-50 border border-gray-200 rounded-xl px-6 py-5">
            <p className="text-xs font-semibold uppercase tracking-widest text-gray-400 mb-3">
              Recommended Next Steps for Production Scoring
            </p>
            <ol className="space-y-1.5 text-xs text-gray-500">
              {[
                "Human-in-the-loop evaluation on 50 queries (gold standard)",
                "LLM-as-judge for claim verification against retrieved chunks",
                "Exact quote matching between cited text and original chunk content",
                "Expected answer coverage per question (precision + recall)",
              ].map((step, i) => (
                <li key={i} className="flex gap-3">
                  <span className="font-mono font-bold text-gray-300 shrink-0">{i + 1}.</span>
                  <span>{step}</span>
                </li>
              ))}
            </ol>
          </div>
        </section>

      </div>

      <footer className="px-6 py-5 border-t border-gray-100 text-xs text-gray-400 text-center mt-6">
        CheckIt Analytics · Evaluation Suite
      </footer>
    </main>
  );
}
