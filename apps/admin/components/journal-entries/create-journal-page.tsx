"use client";

/**
 * Create-journal page shell (US-303): the editor in create mode.
 * After a successful save the operator lands on the new draft's detail
 * page, where validate/post continue the flow.
 */

import * as React from "react";
import { useRouter } from "next/navigation";
import { JournalEditor } from "./journal-editor";

export function CreateJournalPage() {
  const router = useRouter();
  return (
    <JournalEditor
      mode="create"
      onSaved={(draft) => router.push(`/accounting/journals/${draft.id}`)}
      onCancel={() => router.push("/accounting/journals")}
    />
  );
}
