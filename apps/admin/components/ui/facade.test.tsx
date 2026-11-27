import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { MoneyDisplay } from "./money-display";
import { StatusBadge } from "./status-badge";
import { DataTable, type DataTableColumn } from "./data-table";

describe("MoneyDisplay", () => {
  it("shows Toman with an explicit label by default", () => {
    render(<MoneyDisplay amountIrr={12_450_000} />);
    expect(screen.getByText("۱٬۲۴۵٬۰۰۰")).toBeInTheDocument();
    expect(screen.getByText("تومان")).toBeInTheDocument();
  });

  it("shows IRR with an explicit label when asked", () => {
    render(<MoneyDisplay amountIrr={12_450_000} unit="irr" />);
    expect(screen.getByText("۱۲٬۴۵۰٬۰۰۰")).toBeInTheDocument();
    expect(screen.getByText("ریال")).toBeInTheDocument();
  });

  it("renders fractional Toman exactly", () => {
    render(<MoneyDisplay amountIrr={138_555} />);
    expect(screen.getByText("۱۳٬۸۵۵٫۵")).toBeInTheDocument();
  });

  it("rejects non-integer IRR", () => {
    expect(() => render(<MoneyDisplay amountIrr={12.5} />)).toThrow(RangeError);
  });
});

describe("StatusBadge", () => {
  it("maps known states to Persian labels", () => {
    const { rerender } = render(<StatusBadge status="posted" />);
    expect(screen.getByText("ثبت‌شده")).toBeInTheDocument();
    rerender(<StatusBadge status="draft" />);
    expect(screen.getByText("پیش‌نویس")).toBeInTheDocument();
    rerender(<StatusBadge status="paid" />);
    expect(screen.getByText("پرداخت‌شده")).toBeInTheDocument();
  });

  it("renders unknown states as-is instead of hiding them", () => {
    render(<StatusBadge status="mystery_state" />);
    expect(screen.getByText("mystery_state")).toBeInTheDocument();
  });
});

interface Row {
  id: number;
  name: string;
}

const columns: DataTableColumn<Row>[] = [
  { key: "id", header: "شناسه", render: (row) => row.id },
  { key: "name", header: "نام", render: (row) => row.name },
];

describe("DataTable", () => {
  it("renders rows and headers", () => {
    render(
      <DataTable
        columns={columns}
        rows={[
          { id: 1, name: "نقد" },
          { id: 2, name: "بانک" },
        ]}
        rowKey={(row) => row.id}
      />,
    );
    expect(screen.getByText("نقد")).toBeInTheDocument();
    expect(screen.getByText("بانک")).toBeInTheDocument();
    expect(screen.getByText("شناسه")).toBeInTheDocument();
  });

  it("shows the Persian empty state", () => {
    render(<DataTable columns={columns} rows={[]} rowKey={(row) => row.id} />);
    expect(screen.getByText("رکوردی پیدا نشد.")).toBeInTheDocument();
  });

  it("shows skeleton rows while loading", () => {
    const { container } = render(
      <DataTable columns={columns} rows={[]} rowKey={(row) => row.id} loading />,
    );
    expect(container.querySelectorAll('[data-slot="skeleton"]').length).toBeGreaterThan(0);
  });
});
