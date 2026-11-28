export const OPEN_OVERLAY_SELECTOR = [
  '[role="dialog"]',
  '[role="alertdialog"]',
  '[role="listbox"]',
  '[role="menu"]',
  '[data-slot="popover-content"][data-open]',
  '[data-slot="dialog-content"][data-open]',
  '[data-slot="combobox-content"][data-open]',
  ".yg-anchored-panel[data-open]",
  ".yg-popup-menu[data-open]",
  ".yg-popover[data-open]",
  '.yg-inline-command[data-open="true"]',
  '.yg-activity-island[data-open="true"]',
  ".yg-decision-card",
  ".sd-decision-inline",
  '[data-decision-confirm="open"]',
].join(", ");

export function isEditableElement(el: EventTarget | null): boolean {
  if (!(el instanceof Element)) return false;
  if (el instanceof HTMLElement && el.isContentEditable) return true;
  return Boolean(
    el.closest(
      'input, textarea, select, [contenteditable="true"], [contenteditable=""], [role="textbox"]'
    )
  );
}

export function hasOpenOverlay(): boolean {
  const matches = document.querySelectorAll(OPEN_OVERLAY_SELECTOR);
  for (const el of matches) {
    if (el.closest('[inert], [hidden], [aria-hidden="true"], [data-closed]')) {
      continue;
    }
    if (el instanceof HTMLDialogElement && !el.open) {
      continue;
    }
    return true;
  }
  return false;
}

export function focusElementByDataId(focusId: string): boolean {
  const escaped =
    typeof CSS !== "undefined" && typeof CSS.escape === "function"
      ? CSS.escape(focusId)
      : focusId.replace(/"/g, '\\"');
  const target = document.querySelector<HTMLElement>(`[data-focus-id="${escaped}"]`);
  if (!target) return false;
  target.focus();
  return true;
}
