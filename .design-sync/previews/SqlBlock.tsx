import React from "react";
import { SqlBlock } from "ipeds-query-web";

// Every SQL surface in the product renders through SqlBlock: the chat Thinking
// trace, the SQL dropdown, the Skills worked example, and ```sql fences.

export const Formatted = () => (
  <SqlBlock
    code={
      "SELECT stabbr, SUM(ctotalt) AS awards FROM c_a JOIN institutions_current USING (unitid) " +
      "WHERE awlevel = 5 AND majornum = 1 AND cipcode = '51.3801' AND year > (SELECT MAX(year) - 3 FROM _years) " +
      "GROUP BY stabbr ORDER BY awards DESC LIMIT 10;"
    }
  />
);

export const HighlightOnly = () => (
  <SqlBlock
    format={false}
    code={`-- "Recent N years" must be a CONSTANT bound, never a join:
-- a JOIN (SELECT DISTINCT year ...) makes SQLite full-scan the 8M-row c_a.
SELECT year, SUM(ctotalt) AS awards
FROM c_a
WHERE cipcode = '99' AND year > (SELECT MAX(year) - 5 FROM _years)
GROUP BY year
ORDER BY year;`}
  />
);
