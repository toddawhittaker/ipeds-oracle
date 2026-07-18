"""Discovery tools — let the model look up families, columns, and code labels
on demand (the §3 Discovery queries from SCHEMA.md), instead of guessing.

Model-supplied arguments (family/keyword/varname/value) are always passed to
`run_sql` as BOUND parameters, never string-interpolated — so even though these
run on the read-only + immutable sandbox connection, there's no hand-escaping to
get wrong.
"""
from __future__ import annotations

from app.tools.sql import run_sql


def list_families() -> str:
    """Families (unified tables) with row counts and years present."""
    r = run_sql(
        "SELECT family, COUNT(DISTINCT year) AS n_years, "
        "  MIN(year) AS first_year, MAX(year) AS last_year, SUM(n_rows) AS rows "
        "FROM _family_map GROUP BY family ORDER BY family",
        limit=500,
    )
    return r.to_markdown(max_rows=500)


def get_columns(family: str) -> str:
    """Column names of a family (the actual unified columns)."""
    fam = family.strip().strip("'\"")
    r = run_sql("SELECT name, type FROM pragma_table_info(?)", params=(fam,), limit=1000)
    if not r.rows:
        return (f"No family named '{fam}'. Use list_families to see valid names "
                "(query the family name, lowercase — never the year-specific name).")
    return f"Columns of `{fam}`:\n\n" + r.to_markdown(max_rows=1000)


def describe_variables(family: str, keyword: str | None = None) -> str:
    """Variable titles/descriptions for a family (from vartable, latest year)."""
    fam = family.strip().strip("'\"")
    src = run_sql(
        "SELECT src_table FROM _family_map WHERE family = ? ORDER BY year DESC LIMIT 1",
        params=(fam,))
    if not src.rows:
        return f"No family named '{fam}'. Use list_families."
    phys = src.rows[0][0]
    sql = "SELECT varname, vartitle FROM vartable WHERE tablename=?"
    params: list = [phys]
    if keyword:
        sql += " AND (vartitle LIKE '%'||?||'%' OR varname LIKE '%'||?||'%')"
        params += [keyword, keyword]
    sql += " ORDER BY varorder"
    r = run_sql(sql, params=params, limit=400)
    return (f"Variables in `{fam}` (source `{phys}`)"
            + (f" matching '{keyword}'" if keyword else "")
            + ":\n\n" + r.to_markdown(max_rows=400))


def lookup_code(varname: str, value: str | None = None) -> str:
    """Code → label for a categorical variable (valuesets), latest year."""
    var = varname.strip().strip("'\"").upper()
    sql = ("SELECT DISTINCT codevalue, valuelabel FROM valuesets "
           "WHERE UPPER(varname)=? AND year=(SELECT MAX(year) FROM _years)")
    params: list = [var]
    if value is not None:
        sql += " AND codevalue=?"
        params.append(str(value).strip().strip("'\""))
    sql += " ORDER BY LENGTH(codevalue), codevalue"
    r = run_sql(sql, params=params, limit=500)
    if not r.rows:
        return (f"No codes found for variable '{varname}'"
                + (f" value '{value}'" if value else "")
                + ". Check the variable name with describe_variables.")
    return f"Codes for `{varname}`:\n\n" + r.to_markdown(max_rows=500)


def find_variable(keyword: str) -> str:
    """Search all variables by keyword across tables (latest year)."""
    kw = keyword.strip()
    r = run_sql(
        "SELECT DISTINCT tablename, varname, vartitle FROM vartable "
        "WHERE (vartitle LIKE '%'||?||'%' OR varname LIKE '%'||?||'%') "
        "AND year=(SELECT MAX(year) FROM _years) ORDER BY tablename, varname",
        params=(kw, kw), limit=300)
    return f"Variables matching '{keyword}':\n\n" + r.to_markdown(max_rows=300)


def find_cip(keyword: str) -> str:
    """Look up CIP program codes by name (valuesets for CIPCODE)."""
    kw = keyword.strip()
    r = run_sql(
        "SELECT DISTINCT codevalue, valuelabel FROM valuesets "
        "WHERE varname='CIPCODE' AND year=(SELECT MAX(year) FROM _years) "
        "AND valuelabel LIKE '%'||?||'%' ORDER BY codevalue",
        params=(kw,), limit=200)
    return f"CIP codes matching '{keyword}':\n\n" + r.to_markdown(max_rows=200)
