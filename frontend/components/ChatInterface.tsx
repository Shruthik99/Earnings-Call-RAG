"use client";

import { useState, useRef, useEffect, FormEvent } from "react";

const API_BASE = "http://localhost:8001";

// ── Types ────────────────────────────────────────────────────────────────────

interface Evidence {
  chunk_id: string;
  speaker: string;
  quote: string;
  relevance: string;
}

interface AnalysisResult {
  summary: string;
  key_points: string[];
  consistency: "aligned" | "mixed" | "conflict" | "n/a" | string;
  risk_flags: string[];
  confidence: "high" | "medium" | "low" | string;
  evidence: Evidence[] | Evidence;
}

type Message =
  | { role: "user"; content: string }
  | { role: "assistant"; result: AnalysisResult }
  | { role: "assistant"; error: string };

interface Props {
  ticker: string;
  quarter: string;
  year: number;
}

// ── Helpers ──────────────────────────────────────────────────────────────────

function normalizeEvidence(e: Evidence[] | Evidence | undefined): Evidence[] {
  if (!e) return [];
  if (Array.isArray(e)) return e;
  return [e];
}

// chunk_id format: BBY_10K_20260318_chunk_0000 | INTU_8K_20260226_chunk_0001 | BBY_Q3_2026_chunk_000
function sourceLabel(chunkId: string | undefined): string {
  if (!chunkId) return "Source";
  const id = String(chunkId).toUpperCase();
  if (id.includes("_10K_"))          return "10-K Filing";
  if (id.includes("_8K_"))           return "8-K Press Release";
  if (id.includes("_10Q_"))          return "10-Q Filing";
  if (/_(Q1|Q2|Q3|Q4)_/.test(id))   return "Earnings Transcript";
  return "Source";
}

// ── Sub-components ───────────────────────────────────────────────────────────

function ConsistencyBadge({ value }: { value: string }) {
  const val = (value ?? "n/a").toLowerCase();
  const map: Record<string, { cls: string; label: string }> = {
    aligned:  { cls: "bg-green-100 text-green-700",  label: "Aligned"  },
    mixed:    { cls: "bg-yellow-100 text-yellow-800", label: "Mixed"    },
    conflict: { cls: "bg-red-100 text-red-700",       label: "Conflict" },
    "n/a":    { cls: "bg-gray-100 text-gray-500",     label: "N/A"      },
  };
  const { cls, label } = map[val] ?? map["n/a"];
  return (
    <span className={`inline-flex items-center gap-1.5 px-3 py-1 rounded-full text-xs font-semibold ${cls}`}>
      <span className="w-1.5 h-1.5 rounded-full bg-current" />
      {label}
    </span>
  );
}

function ConfidenceBadge({ value }: { value: string }) {
  const val = (value ?? "low").toLowerCase();
  const map: Record<string, string> = {
    high:   "bg-blue-100 text-blue-700",
    medium: "bg-slate-100 text-slate-600",
    low:    "bg-orange-100 text-orange-700",
  };
  const cls = map[val] ?? "bg-gray-100 text-gray-500";
  return (
    <span className={`inline-flex items-center gap-1.5 px-3 py-1 rounded-full text-xs font-semibold ${cls}`}>
      Confidence: {value}
    </span>
  );
}

function AnalysisCard({ result }: { result: AnalysisResult }) {
  const evidence = normalizeEvidence(result.evidence);

  return (
    <div className="space-y-5">
      {/* Summary */}
      <p className="text-lg text-gray-900 leading-relaxed font-normal">{result.summary}</p>

      {/* Key points */}
      {result.key_points?.length > 0 && (
        <div>
          <h4 className="text-[10px] font-semibold uppercase tracking-widest text-gray-400 mb-3">
            Key Points
          </h4>
          <ul className="space-y-2.5 pl-1">
            {result.key_points.map((pt, i) => (
              <li key={i} className="flex gap-3 text-sm text-gray-700">
                <span className="w-1.5 h-1.5 rounded-full bg-blue-400 mt-2 shrink-0" />
                <span className="leading-relaxed">{pt}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* Badges */}
      <div className="flex flex-wrap gap-2">
        <ConsistencyBadge value={result.consistency} />
        <ConfidenceBadge value={result.confidence} />
      </div>

      {/* Risk flags */}
      {result.risk_flags?.length > 0 && (
        <div className="space-y-2">
          <h4 className="text-[10px] font-semibold uppercase tracking-widest text-gray-400">
            Risk Flags
          </h4>
          {result.risk_flags.map((flag, i) => (
            <div
              key={i}
              className="flex gap-2.5 items-start p-3 bg-amber-50 border border-amber-200 rounded-xl text-sm text-amber-900"
            >
              <svg className="w-4 h-4 text-amber-500 shrink-0 mt-0.5" fill="currentColor" viewBox="0 0 20 20">
                <path fillRule="evenodd" d="M8.257 3.099c.765-1.36 2.722-1.36 3.486 0l5.58 9.92c.75 1.334-.213 2.98-1.742 2.98H4.42c-1.53 0-2.493-1.646-1.743-2.98l5.58-9.92zM11 13a1 1 0 11-2 0 1 1 0 012 0zm-1-8a1 1 0 00-1 1v3a1 1 0 002 0V6a1 1 0 00-1-1z" clipRule="evenodd" />
              </svg>
              <span>{flag}</span>
            </div>
          ))}
        </div>
      )}

      {/* Evidence */}
      {evidence.length > 0 && (
        <div>
          <h4 className="text-[10px] font-semibold uppercase tracking-widest text-gray-400 mb-3">
            Evidence ({evidence.length} {evidence.length === 1 ? "citation" : "citations"})
          </h4>
          <div className="space-y-3">
            {evidence.map((e, i) => (
              <div key={i} className="border-l-4 border-blue-500 bg-blue-50 rounded-r-xl pl-4 pr-4 py-3">
                <p className="text-[10px] font-semibold uppercase tracking-wide text-blue-500 mb-1">
                  {sourceLabel(e.chunk_id)}
                </p>
                {e.speaker && (
                  <p className="text-sm font-bold text-gray-900 mb-1">{e.speaker}</p>
                )}
                <blockquote className="text-sm text-gray-700 italic mb-1.5">
                  &ldquo;{e.quote}&rdquo;
                </blockquote>
                <p className="text-xs text-gray-500">{e.relevance}</p>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function ThinkingIndicator() {
  return (
    <div className="flex items-center gap-2 px-4 py-2.5 bg-gray-100 rounded-2xl rounded-tl-sm w-fit text-sm text-gray-500">
      <span className="w-3.5 h-3.5 border-2 border-gray-400 border-t-transparent rounded-full animate-spin shrink-0" />
      Thinking...
    </div>
  );
}

// Raw token stream shown while the model is generating
function StreamingBlock({ text }: { text: string }) {
  return (
    <div className="max-w-[90%] bg-gray-50 border border-gray-200 rounded-2xl rounded-tl-sm px-5 py-4">
      <p className="text-[10px] font-semibold uppercase tracking-widest text-gray-400 mb-2 flex items-center gap-1.5">
        <span className="w-1.5 h-1.5 rounded-full bg-gray-400 animate-pulse" />
        Generating…
      </p>
      <p className="text-sm text-gray-500 font-mono leading-relaxed whitespace-pre-wrap break-words">
        {text}
      </p>
    </div>
  );
}

// ── Main component ───────────────────────────────────────────────────────────

export default function ChatInterface({ ticker, quarter, year }: Props) {
  const [messages, setMessages]       = useState<Message[]>([]);
  const [input, setInput]             = useState("");
  const [isLoading, setIsLoading]     = useState(false);
  const [streamingText, setStreaming] = useState("");
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, isLoading, streamingText]);

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    const question = input.trim();
    if (!question || isLoading) return;

    setInput("");
    setStreaming("");
    setMessages((prev) => [...prev, { role: "user", content: question }]);
    setIsLoading(true);

    try {
      const response = await fetch(`${API_BASE}/api/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ticker, quarter, year, question }),
      });

      if (!response.ok) throw new Error(`Server error ${response.status}`);
      if (!response.body) throw new Error("No response body");

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let currentEvent = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() ?? "";

        for (const line of lines) {
          if (line.startsWith("event: ")) {
            currentEvent = line.slice(7).trim();
          } else if (line.startsWith("data: ")) {
            const raw = line.slice(6);
            if (!raw || raw.startsWith(":")) continue;
            try {
              const payload = JSON.parse(raw);
              if (currentEvent === "token") {
                // Append raw token to the streaming display
                setStreaming((prev) => prev + (typeof payload === "string" ? payload : ""));
              } else if (currentEvent === "result") {
                // Final result: clear stream, add formatted message
                setStreaming("");
                setMessages((prev) => [...prev, { role: "assistant", result: payload }]);
              } else if (currentEvent === "error") {
                setStreaming("");
                setMessages((prev) => [
                  ...prev,
                  { role: "assistant", error: payload.message ?? "Unknown error" },
                ]);
              }
            } catch {
              // malformed JSON line — ignore
            }
          }
        }
      }
    } catch (err) {
      setStreaming("");
      setMessages((prev) => [
        ...prev,
        { role: "assistant", error: String(err) },
      ]);
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <div className="flex flex-col h-full">
      <h3 className="text-[10px] font-semibold uppercase tracking-widest text-gray-400 mb-4">
        Ask about {ticker} {quarter} FY{year}
      </h3>

      {/* Messages */}
      <div className="flex-1 space-y-4 mb-4 overflow-y-auto max-h-[560px] pr-1">
        {messages.length === 0 && !isLoading && (
          <p className="text-sm text-gray-400 text-center py-10">
            Ask a question about this earnings call to get started.
          </p>
        )}

        {messages.map((msg, i) => {
          if (msg.role === "user") {
            return (
              <div key={i} className="flex justify-end">
                <div className="max-w-[75%] bg-blue-600 text-white rounded-2xl rounded-tr-sm px-4 py-2.5 text-sm">
                  {msg.content}
                </div>
              </div>
            );
          }

          if ("error" in msg) {
            return (
              <div key={i} className="flex justify-start">
                <div className="max-w-[85%] bg-red-50 border border-red-200 text-red-700 rounded-2xl rounded-tl-sm px-4 py-2.5 text-sm">
                  Error: {msg.error}
                </div>
              </div>
            );
          }

          return (
            <div key={i} className="flex justify-start">
              <div className="max-w-[90%] bg-white border border-gray-200 rounded-2xl rounded-tl-sm px-6 py-5 shadow-sm">
                <AnalysisCard result={msg.result} />
              </div>
            </div>
          );
        })}

        {/* In-progress: show spinner until first token, then show streaming text */}
        {isLoading && (
          <div className="flex justify-start">
            {streamingText ? (
              <StreamingBlock text={streamingText} />
            ) : (
              <ThinkingIndicator />
            )}
          </div>
        )}

        <div ref={bottomRef} />
      </div>

      {/* Input */}
      <form onSubmit={handleSubmit} className="flex gap-2">
        <input
          type="text"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="e.g. What was said about AI revenue growth?"
          disabled={isLoading}
          className="flex-1 border border-gray-200 rounded-full px-5 py-3 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent disabled:bg-gray-50 disabled:text-gray-400 placeholder-gray-400 transition-shadow duration-200 shadow-sm"
        />
        <button
          type="submit"
          disabled={isLoading || !input.trim()}
          className="px-6 py-3 bg-blue-600 text-white text-sm font-semibold rounded-full hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors duration-200 shadow-sm"
        >
          {isLoading ? "..." : "Ask"}
        </button>
      </form>
    </div>
  );
}
