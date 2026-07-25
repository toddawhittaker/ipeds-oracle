import React from "react";
import { Markdown } from "ipeds-query-web";

// Markdown takes the raw source as a STRING child. Raw HTML is deliberately not
// enabled (this renders model output), so every story is plain GFM.

export const Answer = () => (
  <Markdown>{`Nursing remained the largest bachelor's field in 2024-25, though it is off its 2022 peak.

| Institution | State | Awards |
| --- | --- | ---: |
| Ohio State University-Main Campus | OH | 14,982 |
| University of Illinois Urbana-Champaign | IL | 13,401 |
| The University of Texas at Austin | TX | 12,760 |

*Method: bachelor's only (awlevel 5), first majors, all reporting institutions.*`}</Markdown>
);

export const WithSql = () => (
  <Markdown>{`The query below sums first-major bachelor's awards by state.

\`\`\`sql
SELECT stabbr, SUM(ctotalt) AS awards
FROM c_a JOIN institutions_current USING (unitid)
WHERE awlevel = 5 AND majornum = 1 AND cipcode = '51.3801'
  AND year > (SELECT MAX(year) - 3 FROM _years)
GROUP BY stabbr ORDER BY awards DESC;
\`\`\``}</Markdown>
);

export const Prose = () => (
  <Markdown>{`### What counts as a "first major"

IPEDS reports a completion once per **major**, so a double major appears twice
unless you filter to \`majornum = 1\`. The distinction matters:

- \`majornum = 1\` — first majors only, the usual headline count
- \`majornum = 2\` — second majors, additive to the above

Mixing the two double-counts every dual-major graduate.`}</Markdown>
);
