// Incumbent shim for the phase-3 contest: invokes the incumbent memory
// extension's own searchMemories over its own store, exactly as the
// harness would. Fully env-driven for open install.
// Run via: npx --yes tsx scripts/incumbent-memory.mts --query "..." --k 5
// Env: INCUMBENT_MEMORY_PKG (path to the installed incumbent package,
//      e.g. its src/store modules must be importable under src/store/*.js)
//      INCUMBENT_MEMORY_DIR (path to the incumbent's store directory,
//      the dir that contains sessions.db)
import { resolve } from "node:path";

const pkgDir = process.env.INCUMBENT_MEMORY_PKG;
const memoryDir = process.env.INCUMBENT_MEMORY_DIR;

if (!pkgDir || !memoryDir) {
  console.error(
    "incumbent-memory.mts: set INCUMBENT_MEMORY_PKG and INCUMBENT_MEMORY_DIR",
  );
  process.exit(2);
}

const { DatabaseManager } = await import(resolve(pkgDir, "src", "store", "db.js"));
const { searchMemories } = await import(
  resolve(pkgDir, "src", "store", "sqlite-memory-store.js"),
);

const args = process.argv.slice(2);
const get = (flag: string, fallback?: string) => {
  const i = args.indexOf(flag);
  return i >= 0 ? args[i + 1] : fallback;
};
const query = get("--query");
const k = Number(get("--k", "5"));

if (!query) {
  console.error("usage: incumbent-memory.mts --query <q> --k <n>");
  process.exit(2);
}

const dbManager = new DatabaseManager(memoryDir);
const rows = searchMemories(dbManager, query, { limit: k });
for (const row of rows) {
  console.log(
    JSON.stringify({
      id: row.id,
      content: row.content,
      project: row.project,
      target: row.target,
      category: row.category,
      created: row.created,
      lastReferenced: row.lastReferenced,
    }),
  );
}
