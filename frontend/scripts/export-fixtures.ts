/**
 * Writes shared/fixtures/*.json from the demo data.
 *   npm run export:fixtures            write the files
 *   npm run export:fixtures -- --check  only compare; exit 1 if anything differs
 *
 * The clock is frozen at FIXTURE_NOW before the demo data is loaded, because its
 * dates are relative to "today". That keeps the files identical from day to day.
 */
import { existsSync, mkdirSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const FIXTURE_NOW = "2026-11-14T12:00:00.000Z";
const RealDate = Date;
const fixed = RealDate.parse(FIXTURE_NOW);
class FrozenDate extends RealDate {
  constructor(...args: unknown[]) {
    if (args.length === 0) super(fixed);
    else super(...(args as [string]));
  }
  static now() {
    return fixed;
  }
}
globalThis.Date = FrozenDate as DateConstructor;

const here = dirname(fileURLToPath(import.meta.url));
const outDir = resolve(here, "../../shared/fixtures");
const check = process.argv.includes("--check");

const { buildFixtures, toFileText } = await import("../src/demo/fixtures");
const { manifest, files } = await buildFixtures();
const wanted: Record<string, string> = { "manifest.json": toFileText(manifest) };
for (const [name, value] of Object.entries(files)) wanted[name] = toFileText(value);

if (check) {
  const problems: string[] = [];
  for (const [name, text] of Object.entries(wanted)) {
    const path = join(outDir, name);
    if (!existsSync(path)) problems.push(`missing: ${name}`);
    else if (readFileSync(path, "utf8").replace(/\r\n/g, "\n") !== text) problems.push(`out of date: ${name}`); // CRLF-safe
  }
  if (existsSync(outDir)) {
    for (const name of readdirSync(outDir)) if (name.endsWith(".json") && !(name in wanted)) problems.push(`no longer produced: ${name}`);
  }
  if (problems.length) {
    console.error("Shared fixtures are not up to date. Run: npm run export:fixtures\n  " + problems.join("\n  "));
    process.exit(1);
  }
  console.log(`Shared fixtures are up to date (${Object.keys(wanted).length} files).`);
} else {
  if (existsSync(outDir)) {
    for (const name of readdirSync(outDir)) if (name.endsWith(".json")) rmSync(join(outDir, name));
  }
  mkdirSync(outDir, { recursive: true });
  for (const [name, text] of Object.entries(wanted)) writeFileSync(join(outDir, name), text, "utf8");
  console.log(`Wrote ${Object.keys(wanted).length} files to ${outDir}`);
}
