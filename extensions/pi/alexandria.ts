import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

/**
 * Alexandria memory tools for Pi (SPEC-phase3-harness.md deliverable 1.1).
 *
 * ACTIVATED READ-ONLY 2026-08-08 per principal decision (docs/
 * pi-activation-decision-2026-08-08.md; README phase-3 status). This file is
 * the generic public source; the live install is a private copy at
 * ~/.pi/agent/extensions/alexandria.ts with machine-local paths, a
 * quoted-tilde fix (HOME expansion), a keychain lookup for the gateway key,
 * and the measurement-proven answer config (sonnet-5 + gpt-5.6-terra).
 *
 * Config via environment:
 *   ALEXANDRIA_CORPUS      corpus path (default ~/alexandria-corpus)
 *   ALEXANDRIA_BIN         alexandria binary (default "alexandria")
 *   ALEXANDRIA_SERVE_HOST  warm-server host (default 127.0.0.1)
 *   ALEXANDRIA_SERVE_PORT  warm-server port (default 8420)
 *
 * SPEC-write-path-and-serve.md §5.9 / gate S10: every tool tries a warm
 * `alexandria serve` first and falls back to the CLI exec path when
 * unreachable. A reachable server that returns an HTTP error is a real
 * answer, not an unreachability signal, and is surfaced rather than masked
 * by a silent CLI retry.
 */
export default function (pi: ExtensionAPI) {
  const corpus = process.env.ALEXANDRIA_CORPUS ?? "~/alexandria-corpus";
  const bin = process.env.ALEXANDRIA_BIN ?? "alexandria";
  const serveHost = process.env.ALEXANDRIA_SERVE_HOST ?? "127.0.0.1";
  const servePort = Number(process.env.ALEXANDRIA_SERVE_PORT ?? "8420");

  const run = (cmd: string, timeoutMs: number): string => {
    const { execSync } = require("node:child_process") as typeof import("node:child_process");
    return execSync(cmd, { encoding: "utf-8", timeout: timeoutMs, maxBuffer: 4 * 1024 * 1024 });
  };

  type ServeResult = Record<string, unknown> & { __httpError?: boolean; __status?: number };
  const tryServe = (path: string, body: unknown, timeoutMs: number): Promise<ServeResult | null> => {
    return new Promise((resolve) => {
      const http = require("node:http") as typeof import("node:http");
      const payload = Buffer.from(JSON.stringify(body), "utf-8");
      const req = http.request(
        {
          host: serveHost, port: servePort, path, method: "POST", timeout: timeoutMs,
          headers: { "Content-Type": "application/json", "Content-Length": payload.length },
        },
        (res) => {
          const chunks: Buffer[] = [];
          res.on("data", (c: Buffer) => chunks.push(c));
          res.on("end", () => {
            let parsed: Record<string, unknown> = {};
            try { parsed = JSON.parse(Buffer.concat(chunks).toString("utf-8")); } catch { /* leave {} */ }
            const ok = res.statusCode !== undefined && res.statusCode >= 200 && res.statusCode < 300;
            resolve(ok ? parsed : { ...parsed, __httpError: true, __status: res.statusCode });
          });
        },
      );
      req.on("error", () => resolve(null));           // not listening -- fall back to the CLI
      req.on("timeout", () => { req.destroy(); resolve(null); });
      req.write(payload);
      req.end();
    });
  };

  const formatSearchResults = (results: Array<{ chunk_id: string; heading_path?: string; text: string; score?: number }>): string => {
    if (!results.length) return "no results";
    return results
      .map((r, i) => `${i + 1}. ${r.chunk_id}  score=${(r.score ?? 0).toFixed(6)}\n   ${r.heading_path ?? ""}\n   ${r.text.slice(0, 400)}`)
      .join("\n");
  };

  pi.registerTool({
    name: "alexandria-search",
    label: "Search the Alexandria knowledge base",
    description:
      "Hybrid retrieval over the Alexandria corpus (synthesized wiki pages + sources). " +
      "Use when a question may already have been answered or recorded. Returns ranked " +
      "chunks with source ids. Cheaper than alexandria-answer.",
    parameters: Type.Object({
      query: Type.String({ description: "Search query" }),
      k: Type.Optional(Type.Number({ description: "Result count (default 5)" })),
    }),
    async execute(_toolCallId, params, _signal, _onUpdate, _ctx) {
      const k = params.k ?? 5;
      const served = await tryServe("/search", { query: params.query, k }, 30_000);
      if (served && !served.__httpError) {
        return { content: [{ type: "text", text: formatSearchResults((served.results as any[]) ?? []) }], details: {} };
      }
      if (served && served.__httpError) {
        return { content: [{ type: "text", text: `alexandria-search failed (server, status ${served.__status}): ${JSON.stringify(served)}` }], details: {} };
      }
      try {
        const out = run(`${bin} --corpus "${corpus}" search ${JSON.stringify(params.query)} --k ${k}`, 30_000);
        return { content: [{ type: "text", text: out }], details: {} };
      } catch (err) {
        return { content: [{ type: "text", text: `alexandria-search failed: ${(err as Error).message}` }], details: {} };
      }
    },
  });

  pi.registerTool({
    name: "alexandria-answer",
    label: "Synthesize a cited answer",
    description:
      "Runs the full gather -> write -> judge -> repair pipeline to synthesize a cited " +
      "answer page for a question. More expensive than alexandria-search; prefer search " +
      "first. Requires the LLM gateway config (see docs/QUICKSTART.md).",
    parameters: Type.Object({
      question: Type.String({ description: "Question to answer" }),
    }),
    async execute(_toolCallId, params, _signal, _onUpdate, _ctx) {
      const served = await tryServe("/answer", { question: params.question }, 600_000);
      if (served && !served.__httpError && served.emitted) {
        const prefix = served.cached ? "[cached] " : "";
        return { content: [{ type: "text", text: prefix + String(served.text ?? "") }], details: {} };
      }
      try {
        const out = run(`${bin} --corpus "${corpus}" answer ${JSON.stringify(params.question)}`, 600_000);
        return { content: [{ type: "text", text: out }], details: {} };
      } catch (err) {
        return { content: [{ type: "text", text: `alexandria-answer failed: ${(err as Error).message}` }], details: {} };
      }
    },
  });
}
