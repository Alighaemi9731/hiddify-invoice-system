import { TablePagination } from "@mui/material";

/**
 * A thin wrapper over MUI TablePagination with the project's Persian labels. Hidden when the
 * data fits one page. Pair with a tiny bit of page state:
 *   const [page,setPage]=useState(0); const [rpp,setRpp]=useState(50);
 *   useEffect(()=>setPage(0),[<filter deps>]);
 *   const paged = rows.slice(page*rpp, page*rpp+rpp);
 *   …map paged… then <TablePager count={rows.length} page={page} rpp={rpp} onPage={setPage} onRpp={(v)=>{setRpp(v);setPage(0);}} />
 */
export function TablePager({
  count, page, rpp, onPage, onRpp,
}: {
  count: number;
  page: number;
  rpp: number;
  onPage: (p: number) => void;
  onRpp: (v: number) => void;
}) {
  if (count <= rpp && page === 0) return null;
  return (
    <TablePagination
      component="div"
      count={count}
      page={page}
      rowsPerPage={rpp}
      rowsPerPageOptions={[25, 50, 100]}
      onPageChange={(_, p) => onPage(p)}
      onRowsPerPageChange={(e) => onRpp(parseInt(e.target.value, 10))}
      labelRowsPerPage="تعداد در صفحه:"
      labelDisplayedRows={({ from, to, count: c }) => `${from}–${to} از ${c}`}
    />
  );
}
