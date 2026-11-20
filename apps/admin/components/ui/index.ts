/**
 * Unified UI facade (spec §5.3).
 *
 * Feature pages import ONLY from `@/components/ui` (this barrel) or
 * `@/components/ui/<name>` — never from copied registry files directly.
 * A component replacement happens inside the facade and must not require
 * refactoring every accounting screen.
 *
 * Sources:
 * - FarsiUI (farsiui.ir, MIT, Base UI primitives): button, input, select,
 *   dialog, drawer, sheet, table, badge, skeleton, command, tabs, card,
 *   label, textarea, checkbox, switch, tooltip, popover, dropdown-menu,
 *   scroll-area, separator, alert-dialog, pagination, input-group, sonner.
 * - VibeFarsi (vibefarsi.ir, MIT): calendar, date-picker, amount-input,
 *   price, plus lib/jalali, lib/utils, lib/number-to-words.
 * - Project-owned: data-table, jalali-date-picker, money-display,
 *   status-badge, toast, command-menu (compositions/wrappers above).
 */

// FarsiUI primitives
export { Button, buttonVariants } from "./button";
export { Input } from "./input";
export { Textarea } from "./textarea";
export {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectLabel,
  SelectScrollDownButton,
  SelectScrollUpButton,
  SelectSeparator,
  SelectTrigger,
  SelectValue,
} from "./select";
export {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogOverlay,
  DialogPortal,
  DialogTitle,
  DialogTrigger,
} from "./dialog";
export {
  Drawer,
  DrawerClose,
  DrawerContent,
  DrawerDescription,
  DrawerFooter,
  DrawerHeader,
  DrawerOverlay,
  DrawerPortal,
  DrawerSwipeHandle,
  DrawerTitle,
  DrawerTrigger,
} from "./drawer";
export {
  Sheet,
  SheetClose,
  SheetContent,
  SheetDescription,
  SheetFooter,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from "./sheet";
export {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogMedia,
  AlertDialogOverlay,
  AlertDialogPortal,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "./alert-dialog";
export { Table, TableBody, TableCaption, TableCell, TableFooter, TableHead, TableHeader, TableRow } from "./table";
export { Badge, badgeVariants } from "./badge";
export { Skeleton } from "./skeleton";
export { Label } from "./label";
export { Card, CardAction, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "./card";
export { Tabs, TabsContent, TabsList, TabsTrigger, tabsListVariants } from "./tabs";
export { Checkbox } from "./checkbox";
export { Switch } from "./switch";
export { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "./tooltip";
export { Popover, PopoverContent, PopoverDescription, PopoverHeader, PopoverTitle, PopoverTrigger } from "./popover";
export {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuPortal,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuShortcut,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from "./dropdown-menu";
export { ScrollArea, ScrollBar } from "./scroll-area";
export { Separator } from "./separator";
export {
  Pagination,
  PaginationContent,
  PaginationEllipsis,
  PaginationItem,
  PaginationLink,
  PaginationNext,
  PaginationPrevious,
} from "./pagination";
export {
  InputGroup,
  InputGroupAddon,
  InputGroupButton,
  InputGroupInput,
  InputGroupText,
  InputGroupTextarea,
} from "./input-group";

// VibeFarsi Persian-specific components
export { Calendar } from "./calendar";
export type { CalendarProps } from "./calendar";
export { DatePicker } from "./date-picker";
export type { DatePickerProps } from "./date-picker";
export { AmountInput, shortAmount } from "./amount-input";
export type { AmountInputProps } from "./amount-input";
export { Price } from "./price";
export type { PriceProps } from "./price";

// Project-owned facade compositions
export { DataTable } from "./data-table";
export type { DataTableColumn, DataTableProps } from "./data-table";
export { JalaliDatePicker } from "./jalali-date-picker";
export type { JalaliDatePickerProps } from "./jalali-date-picker";
export { MoneyDisplay } from "./money-display";
export type { MoneyDisplayProps } from "./money-display";
export { StatusBadge, STATUS_MAP } from "./status-badge";
export type { StatusBadgeProps, StatusInfo, StatusTone } from "./status-badge";
export { Toaster, toast } from "./toast";
export { CommandMenu, useCommandMenuShortcut } from "./command-menu";
export type { CommandMenuProps } from "./command-menu";
export {
  Command,
  CommandDialog,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
  CommandSeparator,
  CommandShortcut,
} from "./command-menu";
