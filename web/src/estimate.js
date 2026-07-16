// Disk/time estimator — JS mirror of app/estimate.py's estimate_integrate.
// Cross-language agreement is asserted against the SAME fixture
// (eval/fixtures/estimate_cases.json) by web/e2e/estimate.spec.js; keep this
// in lockstep with the Python original if either changes.
//
// estimateIntegrate takes a SINGLE options object with camelCase keys
// mirroring the Python kwargs 1:1: zipBytes, alreadyIntegratedCount,
// selectedCount, liveDbBytes, currentIntegratedYearCount, diskFreeBytes,
// diskTotalBytes, expandFactor, defaultPerYearDbMb, bandwidthMbps,
// buildSecondsPerYear, safetyFactor. Returns a plain object with camelCase
// output keys mirroring the Python result dict 1:1.
//
// Pinned arithmetic (see eval/test_estimate.py / the fixture for the full
// derivation): MB = 1024*1024 (storage) vs. bandwidthMbps * 1_000_000/8
// (decimal Mbps -> bytes/sec) — these must NOT be conflated. A null zipBytes
// entry contributes exactly one defaultPerYearDbMb*MB slice. perYearDbBytes
// divides liveDbBytes by currentIntegratedYearCount UNLESS either is
// zero/absent, in which case it falls back to the same default. `sufficient`
// is a >=, i.e. free bytes exactly equal to the safety-padded requirement
// still reads as sufficient.

const MB = 1024 * 1024;

function perYearDbBytes(liveDbBytes, currentIntegratedYearCount, defaultPerYearDbMb) {
  if (!liveDbBytes || !currentIntegratedYearCount) {
    return Math.floor(defaultPerYearDbMb * MB);
  }
  return Math.floor(liveDbBytes / currentIntegratedYearCount);
}

export function estimateIntegrate({
  zipBytes,
  alreadyIntegratedCount,
  selectedCount,
  liveDbBytes,
  currentIntegratedYearCount,
  diskFreeBytes,
  diskTotalBytes,
  expandFactor,
  defaultPerYearDbMb,
  bandwidthMbps,
  buildSecondsPerYear,
  safetyFactor,
}) {
  const knownTotal = zipBytes
    .filter((z) => z !== null && z !== undefined)
    .reduce((a, b) => a + b, 0);
  const noneCount = zipBytes.filter((z) => z === null || z === undefined).length;
  const totalDownloadBytes = Math.floor(
    knownTotal + defaultPerYearDbMb * MB * noneCount);

  const extractedBytes = Math.floor(totalDownloadBytes * expandFactor);

  const perYearDb = perYearDbBytes(liveDbBytes, currentIntegratedYearCount, defaultPerYearDbMb);
  const stagingDbBytes = Math.floor(perYearDb * (alreadyIntegratedCount + selectedCount));

  const additionalBytesNeeded = Math.floor(
    totalDownloadBytes + extractedBytes + stagingDbBytes);

  const usedNowBytes = Math.floor(diskTotalBytes - diskFreeBytes);
  const peakUsedBytes = Math.floor(usedNowBytes + additionalBytesNeeded);

  const bandwidthBytesPerSec = (bandwidthMbps * 1_000_000) / 8;
  const estDownloadSeconds = bandwidthBytesPerSec
    ? totalDownloadBytes / bandwidthBytesPerSec
    : 0.0;
  const estBuildSeconds = buildSecondsPerYear * (alreadyIntegratedCount + selectedCount);

  const neededWithSafetyBytes = Math.floor(additionalBytesNeeded * safetyFactor);
  const sufficient = diskFreeBytes >= neededWithSafetyBytes;

  return {
    totalDownloadBytes,
    extractedBytes,
    stagingDbBytes,
    perYearDbBytes: perYearDb,
    additionalBytesNeeded,
    usedNowBytes,
    peakUsedBytes,
    diskFreeBytes,
    diskTotalBytes,
    estDownloadSeconds,
    estBuildSeconds,
    safetyFactor,
    neededWithSafetyBytes,
    sufficient,
  };
}
