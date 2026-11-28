import { formatNsWithClock, normalizeToNs } from "../lib/format";

export { formatNsWithClock, normalizeToNs };

export type SortState = { id: string; descending: boolean } | null;

export interface SortableColumn<T> {
  id: string;
  label: string;
  kind?: "identity" | "text" | "numeric" | "role" | "actions";
  align?: "start" | "end";
  sortValue?: (item: T) => string | number | null | undefined;
}

export function sortItems<T>(
  items: T[],
  sort: SortState,
  cols: SortableColumn<T>[]
): T[] {
  if (!sort) return items;
  const col = cols.find((c) => c.id === sort.id);
  if (!col?.sortValue) return items;
  const getVal = col.sortValue;
  return [...items].sort((a, b) => {
    const av = getVal(a);
    const bv = getVal(b);
    let cmp = 0;
    if (av === null || av === undefined) {
      cmp = bv === null || bv === undefined ? 0 : 1;
    } else if (bv === null || bv === undefined) {
      cmp = -1;
    } else if (typeof av === "number" && typeof bv === "number") {
      cmp = av - bv;
    } else {
      cmp = String(av).localeCompare(String(bv), undefined, { numeric: true });
    }
    return sort.descending ? -cmp : cmp;
  });
}

export function renderSortableTableHead<T>(
  cols: SortableColumn<T>[],
  sort: SortState,
  setSort: (updater: (prev: SortState) => SortState) => void
) {
  return (
    <thead>
      <tr>
        {cols.map((col) => {
          const isNumeric = col.kind === "numeric";
          const isSorted = sort?.id === col.id && Boolean(col.sortValue);
          return (
            <th
              key={col.id}
              scope="col"
              data-kind={col.kind}
              data-align={col.align}
              data-numeric={isNumeric || undefined}
              aria-sort={
                isSorted ? (sort!.descending ? "descending" : "ascending") : undefined
              }
            >
              {col.sortValue ? (
                <button
                  type="button"
                  className="yg-table-sort"
                  onClick={() =>
                    setSort((prev) => ({
                      id: col.id,
                      descending: prev?.id === col.id && !prev.descending,
                    }))
                  }
                >
                  {col.label}
                  <span aria-hidden="true">
                    {isSorted ? (sort!.descending ? "↓" : "↑") : "↕"}
                  </span>
                </button>
              ) : (
                col.label
              )}
            </th>
          );
        })}
      </tr>
    </thead>
  );
}
