// Fails (exit 1) if src/api/schema.gen.ts is not exactly what openapi-typescript generates from the contract.
import { execFileSync } from "node:child_process";
import { readFileSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const dir = mkdtempSync(join(tmpdir(), "oatc-"));
const out = join(dir, "schema.gen.ts");
try {
  const bin = join("node_modules", "openapi-typescript", "bin", "cli.js");
  execFileSync(process.execPath, [bin, "../contracts/openapi/gateway.yaml", "-o", out], { stdio: "pipe" });
  const norm = (s) => s.replace(/\r\n/g, "\n");
  if (norm(readFileSync(out, "utf8")) !== norm(readFileSync("src/api/schema.gen.ts", "utf8"))) {
    console.error("src/api/schema.gen.ts is stale. Run: npm run gen:api");
    process.exit(1);
  }
  console.log("api types up to date");
} finally {
  rmSync(dir, { recursive: true, force: true });
}
