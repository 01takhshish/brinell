"use client";

import { useState } from "react";

import { JsonBlock } from "@/components/json-block";
import { formatDuration, formatTokens, formatUsd } from "@/lib/format";
import type { Span } from "@/lib/types";

/**
 * The agent's execution, step by step.
 *
 * Laid out as a waterfall rather than a list because the question people bring to a slow case is
 * "which step took the time", and that is a question about proportion. Bars are scaled to the
 * longest span in the case, so the widest bar is always the thing to look at first.
 */
export function Trace({ spans }: { spans: Span[] }) {
  const longest = Math.max(1, ...spans.map((s) => s.duration_ms ?? 0));

  return (
    <ol className="divide-y">
      {spans.map((span, index) => (
        <TraceRow key={index} span={span} index={index} longest={longest} />
      ))}
    </ol>
  );
}

function TraceRow({ span, index, longest }: { span: Span; index: number; longest: number }) {
  const [open, setOpen] = useState(false);
  const duration = span.duration_ms ?? 0;
  const width = Math.max(1.5, (duration / longest) * 100);
  const kind = (span.kind ?? span.name ?? "step").toString();
  const isTool = /tool/i.test(kind);
  const failed = Boolean(span.error);

  return (
    <li>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex w-full items-center gap-3 px-4 py-2 text-left hover:bg-bg"
      >
        <span className="num w-5 shrink-0 text-xs text-faint">{index + 1}</span>

        <span className="flex w-40 shrink-0 items-center gap-2">
          <span
            className={`size-1.5 shrink-0 rounded-full ${
              failed ? "bg-fail" : isTool ? "bg-info" : "bg-accent"
            }`}
          />
          <span className="truncate text-sm font-medium">{span.name ?? kind}</span>
        </span>

        <span className="hidden min-w-0 flex-1 items-center sm:flex">
          <span className="h-1.5 w-full overflow-hidden rounded-full bg-border">
            <span
              className={`block h-full rounded-full ${failed ? "bg-fail" : isTool ? "bg-info" : "bg-accent"}`}
              style={{ width: `${width}%` }}
            />
          </span>
        </span>

        <span className="num w-20 shrink-0 text-right text-xs text-muted">
          {formatDuration(span.duration_ms)}
        </span>
        <span className="num hidden w-24 shrink-0 text-right text-xs text-muted md:block">
          {span.input_tokens === undefined && span.output_tokens === undefined
            ? "—"
            : `${formatTokens(span.input_tokens ?? 0)}/${formatTokens(span.output_tokens ?? 0)}`}
        </span>
        <span className="num hidden w-20 shrink-0 text-right text-xs text-muted lg:block">
          {span.cost_micros === undefined ? "—" : formatUsd(span.cost_micros)}
        </span>
        <span className="w-14 shrink-0 text-right text-xs text-faint">{open ? "hide" : "open"}</span>
      </button>

      {open && (
        <div className="space-y-2 border-t bg-bg/50 px-4 py-3">
          <dl className="flex flex-wrap gap-x-6 gap-y-1 text-xs text-muted">
            {span.model && (
              <div>
                <dt className="inline text-faint">model </dt>
                <dd className="num inline">{span.model}</dd>
              </div>
            )}
            {span.cache_hit && <div className="text-info">served from cache</div>}
          </dl>
          {span.error && (
            <p className="wrap-anywhere rounded border border-fail/40 bg-fail-bg px-2.5 py-1.5 font-mono text-xs text-fail">
              {span.error}
            </p>
          )}
          {span.input !== undefined && <JsonBlock label="input" value={span.input} />}
          {span.output !== undefined && <JsonBlock label="output" value={span.output} />}
        </div>
      )}
    </li>
  );
}
