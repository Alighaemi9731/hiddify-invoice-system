import { useMemo, useState } from "react";
import { ResellerRow, ResellerTreeRow } from "../../api/client";
import { Dir } from "../../components/sortable";

export type VisibleTreeRow = { node: ResellerTreeRow; depth: number };

export const countTree = (nodes: ResellerTreeRow[]): number =>
  nodes.reduce((total, node) => total + 1 + countTree(node.children || []), 0);

const branchIds = (nodes: ResellerTreeRow[]): number[] =>
  nodes.flatMap((node) => [
    ...(node.children?.length ? [node.id] : []),
    ...branchIds(node.children || []),
  ]);

function flattenVisible(
  nodes: ResellerTreeRow[],
  expanded: Set<number>,
  depth = 0,
): VisibleTreeRow[] {
  const rows: VisibleTreeRow[] = [];
  for (const node of nodes) {
    rows.push({ node, depth });
    if (expanded.has(node.id) && node.children?.length) {
      rows.push(...flattenVisible(node.children, expanded, depth + 1));
    }
  }
  return rows;
}

function compareRows(a: ResellerRow, b: ResellerRow, key: string, dir: Dir) {
  const av = (a as any)[key];
  const bv = (b as any)[key];
  if (av == null && bv == null) return 0;
  if (av == null) return 1;
  if (bv == null) return -1;

  let result: number;
  if (typeof av === "number" && typeof bv === "number") result = av - bv;
  else if (typeof av === "boolean" && typeof bv === "boolean") {
    result = Number(av) - Number(bv);
  } else {
    result = String(av).localeCompare(String(bv), "fa");
  }
  return dir === "asc" ? result : -result;
}

function sortTree(
  nodes: ResellerTreeRow[],
  key: string,
  dir: Dir,
): ResellerTreeRow[] {
  return [...nodes]
    .sort((a, b) => compareRows(a, b, key, dir))
    .map((node) => ({
      ...node,
      children: sortTree(node.children || [], key, dir),
    }));
}

/**
 * Tree presentation state: sorts roots and each sibling group recursively
 * (preserving parent-child grouping), pages by root branch, flattens only the
 * expanded branches into visible rows, and owns the expansion set.
 */
export function useResellerTree(
  tree: ResellerTreeRow[],
  key: string,
  dir: Dir,
  page: number,
) {
  const [expanded, setExpanded] = useState<Set<number>>(new Set());

  const sortedTree = useMemo(
    () => sortTree(tree, key, dir),
    [tree, key, dir],
  );
  const pagedTree = useMemo(
    () => sortedTree.slice(page * 25, page * 25 + 25),
    [sortedTree, page],
  );
  const visibleTreeRows = useMemo(
    () => flattenVisible(pagedTree, expanded),
    [pagedTree, expanded],
  );
  const allBranchIds = useMemo(() => branchIds(pagedTree), [pagedTree]);

  const toggleBranch = (id: number) => {
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  return { expanded, setExpanded, toggleBranch, visibleTreeRows, allBranchIds };
}
