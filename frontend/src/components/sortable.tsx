import { useMemo, useState } from "react";
import { TableCell, TableSortLabel } from "@mui/material";

export type Dir = "asc" | "desc";

export function useSort<T>(rows: T[], initialKey: string, initialDir: Dir = "desc") {
  const [key, setKey] = useState(initialKey);
  const [dir, setDir] = useState<Dir>(initialDir);

  const toggle = (k: string) => {
    if (k === key) setDir((d) => (d === "asc" ? "desc" : "asc"));
    else { setKey(k); setDir("asc"); }
  };

  const sorted = useMemo(() => {
    const arr = [...(rows || [])];
    arr.sort((a: any, b: any) => {
      const av = a?.[key];
      const bv = b?.[key];
      if (av == null && bv == null) return 0;
      if (av == null) return 1;
      if (bv == null) return -1;
      let r: number;
      if (typeof av === "number" && typeof bv === "number") r = av - bv;
      else if (typeof av === "boolean" && typeof bv === "boolean") r = Number(av) - Number(bv);
      else r = String(av).localeCompare(String(bv), "fa");
      return dir === "asc" ? r : -r;
    });
    return arr;
  }, [rows, key, dir]);

  return { sorted, key, dir, toggle };
}

export function SortTh({
  id, label, sortKey, dir, onSort, align,
}: {
  id: string; label: string; sortKey: string; dir: Dir;
  onSort: (k: string) => void; align?: "left" | "right" | "center";
}) {
  return (
    <TableCell align={align} sortDirection={sortKey === id ? dir : false}>
      <TableSortLabel
        active={sortKey === id}
        direction={sortKey === id ? dir : "asc"}
        onClick={() => onSort(id)}
      >
        {label}
      </TableSortLabel>
    </TableCell>
  );
}
