// Proves demo data never ships in a normal build:
//   normal build -> must NOT contain the demo marker
//   demo build   -> must contain it (so the check itself works)
// Run: npm run test:build
import { execSync } from "node:child_process";
import { readFileSync, readdirSync, rmSync } from "node:fs";
import { join } from "node:path";

const MARKER = "veritas-lens-demo-data-v1";

function build(mode, outDir) {
  rmSync(outDir, { recursive: true, force: true });
  execSync(`npx vite build --mode ${mode} --outDir ${outDir} --emptyOutDir --logLevel error`, { stdio: "inherit" });
  const files = readdirSync(join(outDir, "assets")).filter((f) => f.endsWith(".js"));
  const found = files.some((f) => readFileSync(join(outDir, "assets", f), "utf8").includes(MARKER));
  return { files: files.length, found };
}

let failed = false;
const normal = build("production", "dist-check-normal");
console.log(`normal build: ${normal.files} js files, demo data ${normal.found ? "FOUND" : "absent"}`);
if (normal.found) failed = true;

const demo = build("demo", "dist-check-demo");
console.log(`demo build:   ${demo.files} js files, demo data ${demo.found ? "present" : "MISSING"}`);
if (!demo.found) failed = true;

rmSync("dist-check-normal", { recursive: true, force: true });
rmSync("dist-check-demo", { recursive: true, force: true });

if (failed) {
  console.error("Demo isolation check FAILED.");
  process.exit(1);
}
console.log("Demo isolation check passed.");
