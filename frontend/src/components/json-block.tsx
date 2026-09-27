"use client";

import { useState } from "react";

const COLLAPSE_OVER_LINES = 14;

/**
 * Agent inputs and outputs are unbounded — a ReAct transcript can be thousands of lines — so
 * anything long is collapsed by default. The alternative is a case detail page where finding the
 * assertion results means scrolling past a wall of JSON.
 */
export function JsonBlock({ value, label }: { value: unknown; label?: string }) {
  const text = stringify(value);
  const lines = text.split("\n");
  const collapsible = lines.length > COLLAPSE_OVER_LINES;
  const [expanded, setExpanded] = useState(false);
  const shown = collapsible && !expanded ? lines.slice(0, COLLAPSE_OVER_LINES).join("\n") : text;

  return (
    <div className="rounded border bg-bg">
      {label && (
        <div className="flex items-center justify-between border-b px-2.5 py-1.5">
          <span className="text-xs font-medium text-muted">{label}</span>
          {collapsible && (
            <button
              type="button"
              onClick={() => setExpanded((e) => !e)}
              className="text-xs text-accent hover:underline"
            >
              {expanded ? "collapse" : `show all ${lines.length} lines`}
            </button>
          )}
        </div>
      )}
      <pre className="wrap-anywhere max-h-[32rem] overflow-auto px-2.5 py-2 font-mono text-xs leading-relaxed whitespace-pre-wrap">
        {shown}
        {collapsible && !expanded && <span className="text-faint">{"\n…"}</span>}
      </pre>
    </div>
  );
}

function stringify(value: unknown): string {
  if (value === null || value === undefined) return "null";
  // A model's answer is usually a bare string, and JSON.stringify would render it with quotes and
  // escaped newlines — unreadable for exactly the field people most want to read.
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}
