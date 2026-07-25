import React from "react";
import { DataTable } from "ipeds-query-web";

// DataTable owns search, sort and paging client-side, so every story hands it
// the WHOLE row set unpaginated — that is the real calling convention.
//
// `config` MUST carry comparators + tiebreak, not just fields/nouns: sortRows
// does Object.keys(comparators) and the table renders blank without them. And
// `rowKey` is an accessor function, not a field name.

const ROWS = [
  { unitid: 201885, instnm: "Ohio State University-Main Campus", stabbr: "OH", awards: 14_982 },
  { unitid: 145637, instnm: "University of Illinois Urbana-Champaign", stabbr: "IL", awards: 13_401 },
  { unitid: 228778, instnm: "The University of Texas at Austin", stabbr: "TX", awards: 12_760 },
  { unitid: 170976, instnm: "University of Michigan-Ann Arbor", stabbr: "MI", awards: 11_884 },
  { unitid: 110635, instnm: "University of California-Berkeley", stabbr: "CA", awards: 11_302 },
  { unitid: 204796, instnm: "Franklin University", stabbr: "OH", awards: 2_140 },
];

const COLUMNS = [
  { key: "instnm", label: "Institution", sortable: true },
  { key: "stabbr", label: "State", sortable: true },
  {
    key: "awards",
    label: "Awards conferred",
    sortable: true,
    thClass: "num",
    cellClass: "num",
    render: (r: any) => r.awards.toLocaleString(),
  },
];

const text = (k: string) => (a: any, b: any) =>
  String(a[k] ?? "").localeCompare(String(b[k] ?? ""), undefined, { sensitivity: "base" });

const CONFIG = {
  fields: ["instnm", "stabbr"],
  // Default-sort column first: an unknown sortKey falls back to this one.
  comparators: {
    awards: (a: any, b: any) => a.awards - b.awards,
    instnm: text("instnm"),
    stabbr: text("stabbr"),
  },
  tiebreak: (r: any) => r.unitid,
  nouns: { one: "institution", many: "institutions" },
};

const rowKey = (r: any) => r.unitid;

export const Sortable = () => (
  <DataTable
    rows={ROWS}
    columns={COLUMNS}
    rowKey={rowKey}
    config={CONFIG}
    ariaLabel="Institutions by awards conferred"
    initialSort={{ key: "awards", dir: "desc" }}
    searchPlaceholder="Search institutions"
    searchLabel="Search institutions"
    sizeLabel="Institutions per page"
  />
);

export const WithRowActions = () => (
  <DataTable
    rows={ROWS.slice(0, 4)}
    columns={COLUMNS}
    rowKey={rowKey}
    config={CONFIG}
    ariaLabel="Institutions with row actions"
    renderActions={(r: any) => (
      <button type="button" className="link">
        View {r.stabbr}
      </button>
    )}
  />
);

export const Empty = () => (
  <DataTable
    rows={[]}
    columns={COLUMNS}
    rowKey={rowKey}
    config={CONFIG}
    ariaLabel="No institutions"
    emptyNoData="No institutions loaded yet."
  />
);
