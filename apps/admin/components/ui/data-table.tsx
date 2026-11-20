"use client";

/**
 * Data table (facade) — project-owned composition on the FarsiUI Table,
 * Pagination and Skeleton primitives (no data-table exists in either
 * registry; spec §5.2 reserves report/data tables as project-owned).
 *
 * The API is cursor-paginated (forward-only `next_cursor`), so the table
 * offers a "next page" action, not numbered pages. Sorting and filtering
 * live in the feature page, using normalized values — never localized
 * display strings (spec §5.5 rule 10).
 *
 * Rows with an onRowClick are keyboard-operable too (US-305): the row
 * is focusable and Enter/Space activates it, so a mouse is never
 * required to open a record. Keys pressed inside a nested control
 * (link/button in a cell) are left alone.
 */

import * as React from "react";
import { cn } from "@/lib/utils";
import { Button } from "./button";
import { Skeleton } from "./skeleton";
import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from "./table";

export interface DataTableColumn<T> {
  key: string;
  header: React.ReactNode;
  /** Render a cell from the row. */
  render: (row: T, index: number) => React.ReactNode;
  className?: string;
  headerClassName?: string;
}

export interface DataTableProps<T> {
  columns: DataTableColumn<T>[];
  rows: T[];
  /** Stable row key (normalized id, never a display string). */
  rowKey: (row: T, index: number) => string | number;
  /** Initial load: renders skeleton rows shaped like the table. */
  loading?: boolean;
  /** Persian empty-state text. */
  emptyText?: string;
  /** Cursor pagination: more rows exist after this page. */
  hasNextPage?: boolean;
  onNextPage?: () => void;
  /** Loading the next page (keeps current rows visible). */
  loadingMore?: boolean;
  onRowClick?: (row: T) => void;
  caption?: string;
  className?: string;
}

const SKELETON_ROWS = 5;

export function DataTable<T>({
  columns,
  rows,
  rowKey,
  loading = false,
  emptyText = "رکوردی پیدا نشد.",
  hasNextPage = false,
  onNextPage,
  loadingMore = false,
  onRowClick,
  caption,
  className,
}: DataTableProps<T>) {
  return (
    <div className={cn("w-full", className)}>
      <div className="overflow-x-auto rounded-lg border">
        <Table>
          {caption ? <TableCaption>{caption}</TableCaption> : null}
          <TableHeader>
            <TableRow>
              {columns.map((col) => (
                <TableHead key={col.key} className={col.headerClassName}>
                  {col.header}
                </TableHead>
              ))}
            </TableRow>
          </TableHeader>
          <TableBody>
            {loading ? (
              Array.from({ length: SKELETON_ROWS }).map((_, r) => (
                <TableRow key={`skeleton-${r}`}>
                  {columns.map((col) => (
                    <TableCell key={col.key}>
                      <Skeleton className="h-4 w-full" />
                    </TableCell>
                  ))}
                </TableRow>
              ))
            ) : rows.length === 0 ? (
              <TableRow>
                <TableCell colSpan={columns.length} className="py-12 text-center text-muted-foreground">
                  {emptyText}
                </TableCell>
              </TableRow>
            ) : (
              rows.map((row, i) => (
                <TableRow
                  key={rowKey(row, i)}
                  onClick={onRowClick ? () => onRowClick(row) : undefined}
                  tabIndex={onRowClick ? 0 : undefined}
                  onKeyDown={
                    onRowClick
                      ? (event) => {
                          // Only the row itself activates: a key pressed
                          // on a nested link/button belongs to that control.
                          if (event.target !== event.currentTarget) return;
                          if (event.key === "Enter" || event.key === " ") {
                            event.preventDefault();
                            onRowClick(row);
                          }
                        }
                      : undefined
                  }
                  className={onRowClick ? "cursor-pointer" : undefined}
                >
                  {columns.map((col) => (
                    <TableCell key={col.key} className={col.className}>
                      {col.render(row, i)}
                    </TableCell>
                  ))}
                </TableRow>
              ))
            )}
          </TableBody>
        </Table>
      </div>
      {hasNextPage && onNextPage && !loading ? (
        <div className="mt-4 flex justify-center">
          <Button variant="outline" onClick={onNextPage} disabled={loadingMore}>
            {loadingMore ? "در حال بارگذاری…" : "صفحهٔ بعد"}
          </Button>
        </div>
      ) : null}
    </div>
  );
}
