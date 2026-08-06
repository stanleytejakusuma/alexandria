import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

/**
 * Alexandria memory tools for Pi (SPEC-phase3-harness.md deliverable 1.1).
 *
 * INERT BY DESIGN: per the phase-3 spec, nothing switches until the blinded
 * side-by-side gate passes. This file is the shipped source; install it at
 * ~/.pi/agent/extensions/alexandria.ts ONLY after a PASS verdict (Stanley's
 * call), or earlier for test-only sessions with the caveat recorded.
 *
 * Config via environment:
 *   ALEXANDRIA_CORPUS  corpus path (default ~/alexandria-corpus)
 *   ALEXANDRIA_BIN     alexandria binary (default "alexandria")
 */
export default function (pi: ExtensionAPI) {
  const corpus = process.env.ALEXANDRIA_CORPUS ?? "~/alexandria-corpus";
  const bin = process.env.ALEXANDRIA_BIN ?? "alexandria";

  const run = (cmd: string, timeoutMs: number): string => {
    const { execSync } = require("node:child_process") as typeof import("node:child_process");
    return execSync(cmd, { encoding: "utf-8", timeout: timeoutMs, maxBuffer: 4 * 1024 * 1024 });
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
      try {
        const k = params.k ?? 5;
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
      try {
        const out = run(`${bin} --corpus "${corpus}" answer ${JSON.stringify(params.question)}`, 600_000);
        return { content: [{ type: "text", text: out }], details: {} };
      } catch (err) {
        return { content: [{ type: "text", text: `alexandria-answer failed: ${(err as Error).message}` }], details: {} };
      }
    },
  });
}
