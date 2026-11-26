import type { MouseEvent } from "react";
import { Breadcrumb, type Destination } from "@openboa/yogurt";
import { useApp } from "../state/store";

interface SpaBreadcrumbProps {
  items: Destination[];
  label?: string;
  focusReturnId?: string;
}

export function SpaBreadcrumb({ items, label, focusReturnId }: SpaBreadcrumbProps) {
  const { navigate } = useApp();

  const handleClick = (event: MouseEvent<HTMLDivElement>) => {
    if (
      event.defaultPrevented ||
      event.button !== 0 ||
      event.metaKey ||
      event.ctrlKey ||
      event.shiftKey ||
      event.altKey
    ) {
      return;
    }
    const target = event.target;
    if (!(target instanceof Element)) return;
    const anchor = target.closest("a[href]");
    if (!(anchor instanceof HTMLAnchorElement)) return;
    const href = anchor.getAttribute("href");
    if (!href || !href.startsWith("/")) return;
    event.preventDefault();
    navigate(href, focusReturnId ? { focusReturnId } : undefined);
  };

  return (
    <div className="sd-breadcrumb-wrap" onClick={handleClick}>
      <Breadcrumb items={items} label={label} />
    </div>
  );
}
