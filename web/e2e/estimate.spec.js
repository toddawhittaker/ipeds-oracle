// Cross-language agreement test: web/src/estimate.js's estimateIntegrate must
// reproduce app/estimate.py's estimate_integrate EXACTLY, byte-for-byte (ints)
// and to float precision (seconds), against the SHARED ground-truth fixture
// eval/fixtures/estimate_cases.json (also asserted from the Python side by
// eval/test_estimate.py). See that fixture's cases for the derivation.
//
// The fixture stores snake_case keys (the Python function's kwarg names).
// DECISION (documented here since the architect's contract left the exact
// JS calling convention open): estimateIntegrate takes a SINGLE options
// object with camelCase keys mirroring the Python kwargs 1:1 (zipBytes,
// alreadyIntegratedCount, selectedCount, liveDbBytes, currentIntegratedYearCount,
// diskFreeBytes, diskTotalBytes, expandFactor, defaultPerYearDbMb,
// bandwidthMbps, buildSecondsPerYear, safetyFactor) and returns a plain object
// with camelCase output keys (totalDownloadBytes, extractedBytes,
// stagingDbBytes, perYearDbBytes, additionalBytesNeeded, usedNowBytes,
// peakUsedBytes, diskFreeBytes, diskTotalBytes, estDownloadSeconds,
// estBuildSeconds, safetyFactor, neededWithSafetyBytes, sufficient) — this
// test converts the fixture's snake_case keys to camelCase rather than the
// fixture storing both shapes, so there is exactly one ground truth file.
import { test, expect } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { estimateIntegrate } from "../src/estimate.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const FIXTURE_PATH = path.resolve(__dirname, "../../eval/fixtures/estimate_cases.json");

function snakeToCamel(key) {
  return key.replace(/_([a-zA-Z0-9])/g, (_, c) => c.toUpperCase());
}

function camelizeKeys(obj) {
  const out = {};
  for (const [k, v] of Object.entries(obj)) {
    out[snakeToCamel(k)] = v;
  }
  return out;
}

const cases = JSON.parse(fs.readFileSync(FIXTURE_PATH, "utf-8"));

test.describe("estimateIntegrate (JS) agrees with app.estimate.estimate_integrate (Python)", () => {
  test(`fixture has all 4 required cases`, () => {
    expect(cases.length).toBe(4);
    const names = new Set(cases.map((c) => c.name));
    expect(names).toEqual(new Set([
      "normal_multi_year_selection",
      "none_zip_uses_default_per_year_mb",
      "divide_by_zero_fallback_per_year_db_bytes",
      "sufficient_boundary_exact_equal_is_true",
    ]));
  });

  for (const { name, input, expected } of cases) {
    test(`case: ${name}`, () => {
      const jsInput = camelizeKeys(input);
      const jsExpected = camelizeKeys(expected);
      const result = estimateIntegrate(jsInput);

      expect(new Set(Object.keys(result))).toEqual(new Set(Object.keys(jsExpected)));

      for (const [key, expVal] of Object.entries(jsExpected)) {
        const got = result[key];
        if (typeof expVal === "number" && !Number.isInteger(expVal)) {
          // Floats (est_download_seconds etc.) — tolerate float rounding.
          expect(got, `${name}.${key}`).toBeCloseTo(expVal, 6);
        } else if (typeof expVal === "boolean") {
          expect(got, `${name}.${key}`).toBe(expVal);
        } else {
          // Exact byte counts (floor'd ints) must match exactly.
          expect(got, `${name}.${key}`).toBe(expVal);
        }
      }
    });
  }

  test("sufficient boundary case: free bytes exactly equal to needed -> true", () => {
    const boundary = cases.find((c) => c.name === "sufficient_boundary_exact_equal_is_true");
    const result = estimateIntegrate(camelizeKeys(boundary.input));
    expect(result.diskFreeBytes).toBe(result.neededWithSafetyBytes);
    expect(result.sufficient).toBe(true);
  });

  test("one byte short of the boundary flips sufficient to false", () => {
    const boundary = cases.find((c) => c.name === "sufficient_boundary_exact_equal_is_true");
    const jsInput = camelizeKeys(boundary.input);
    jsInput.diskFreeBytes = boundary.expected.needed_with_safety_bytes - 1;
    const result = estimateIntegrate(jsInput);
    expect(result.sufficient).toBe(false);
  });

  test("bandwidth uses decimal Mbps/8, not the 1024*1024 storage MB constant", () => {
    // 10 Mbps == 1,250,000 bytes/sec (decimal). A 1,250,000-byte download
    // must take exactly 1.0 second — pins that the JS mirror doesn't reuse
    // the 1024*1024 byte-storage constant for the bandwidth conversion too.
    const result = estimateIntegrate({
      zipBytes: [1_250_000], alreadyIntegratedCount: 0, selectedCount: 1,
      liveDbBytes: 0, currentIntegratedYearCount: 0,
      diskFreeBytes: 10_000_000_000, diskTotalBytes: 10_000_000_000,
      expandFactor: 1.0, defaultPerYearDbMb: 0, bandwidthMbps: 10.0,
      buildSecondsPerYear: 0.0, safetyFactor: 1.0,
    });
    expect(result.estDownloadSeconds).toBeCloseTo(1.0, 9);
  });

  test("a null zip entry contributes exactly one defaultPerYearDbMb*MB slice", () => {
    const MB = 1024 * 1024;
    const result = estimateIntegrate({
      zipBytes: [300_000_000, null], alreadyIntegratedCount: 1, selectedCount: 1,
      liveDbBytes: 1_000_000_000, currentIntegratedYearCount: 2,
      diskFreeBytes: 5_000_000_000, diskTotalBytes: 20_000_000_000,
      expandFactor: 3.0, defaultPerYearDbMb: 380, bandwidthMbps: 10.0,
      buildSecondsPerYear: 60.0, safetyFactor: 1.2,
    });
    expect(result.totalDownloadBytes).toBe(300_000_000 + 380 * MB);
  });
});
