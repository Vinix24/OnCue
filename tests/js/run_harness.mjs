/**
 * Deterministic entry point for the dashboard JS harness.
 *
 * `node --test` picks its reporter from the environment: this repo's pinned
 * Node 22.22.3 emits TAP into a pipe locally, while the same major on the CI
 * runner emitted the `spec` reporter (`ℹ pass 26`) into the same pipe. A wrapper
 * that reads a reporter's prose is therefore tuned to one build and reports a
 * green harness as a failure on another, which is exactly what happened to
 * PR #214.
 *
 * So no reporter is parsed at all. This runner consumes node:test's programmatic
 * event stream -- structured objects, not text -- and prints a small summary in
 * a shape this repo owns, then exits non-zero on any failure. The Python wrapper
 * (tests/test_dashboard_js.py) asserts on the exit code plus those counts.
 *
 * It fails, rather than passing quietly, when a test file contributes no tests
 * at all: a harness that cannot tell "green" from "never ran" is worth less than
 * no harness.
 *
 * Still only node: built-ins (node:test, node:fs, node:path, node:url), so the
 * "no package.json, no node_modules, no framework" invariant that
 * scripts/check_architecture_boundaries.py enforces holds unchanged.
 *
 * Run it directly for the human-readable version:
 *     node tests/js/run_harness.mjs
 */

import { readdirSync } from "node:fs";
import { basename, dirname, resolve } from "node:path";
import { run } from "node:test";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));

// Discovered, not hardcoded: the Python wrapper globs the same pattern
// independently and compares counts, so a new test file that one side forgets
// shows up as a mismatch instead of silently not running.
const files = readdirSync(HERE)
  .filter((name) => name.endsWith(".test.mjs"))
  .sort()
  .map((name) => resolve(HERE, name));

if (files.length === 0) {
  console.error("run_harness: no *.test.mjs files found in tests/js/");
  process.exit(1);
}

const passedPerFile = new Map(files.map((file) => [file, 0]));
const failures = [];

const isSuite = (data) => data?.details?.type === "suite";

// A file that registers NO tests still emits one passing event of its own,
// whose `name` is the file path rather than a test name (measured on Node
// 20.18.2, 22.22.3 and 26.7.0). Counting that phantom would report a file that
// never ran anything as a green test -- precisely the "cannot tell green from
// missing" failure this runner exists to prevent. A real test's name is never
// its own absolute path, so that is the discriminator.
const isFileRollup = (data) => data?.name === data?.file;

const stream = run({ files, concurrency: 1 });

// Iterate the stream rather than binding named events: reading it is what makes
// it flow, and each chunk is a {type, data} object -- the machine-readable form.
for await (const event of stream) {
  if (event.type === "test:pass" && !isSuite(event.data) && !isFileRollup(event.data)) {
    passedPerFile.set(event.data.file, (passedPerFile.get(event.data.file) || 0) + 1);
  } else if (event.type === "test:fail" && !isSuite(event.data)) {
    failures.push({
      name: event.data.name,
      file: event.data.file,
      error: event.data.details?.error,
    });
  } else if (event.type === "test:stderr") {
    process.stderr.write(event.data.message);
  }
}

const passed = [...passedPerFile.values()].reduce((total, count) => total + count, 0);
const emptyFiles = files.filter((file) => (passedPerFile.get(file) || 0) === 0 && !failures.some((f) => f.file === file));

for (const failure of failures) {
  console.log(`FAIL ${basename(failure.file || "?")} :: ${failure.name}`);
  const error = failure.error;
  console.log(`     ${error?.message || error || "(no error message)"}`);
  if (error?.stack) {
    console.log(String(error.stack).split("\n").slice(0, 6).map((line) => `     ${line}`).join("\n"));
  }
}

for (const file of emptyFiles) {
  console.log(`FAIL ${basename(file)} :: contributed no tests`);
}

// The summary this repo owns. Parsed by tests/test_dashboard_js.py.
console.log(`HARNESS_NODE ${process.version}`);
console.log(`HARNESS_FILES ${files.length}`);
console.log(`HARNESS_PASS ${passed}`);
console.log(`HARNESS_FAIL ${failures.length + emptyFiles.length}`);

process.exit(failures.length === 0 && emptyFiles.length === 0 && passed > 0 ? 0 : 1);
