import React, { useEffect, useId, useRef, useState } from "react";
import { IconCopy, IconCheck, IconChevronDown } from "./icons.jsx";

// The assistant answer's "Copy" control. The action row used to carry TWO
// full-width text buttons ("Copy Markdown" + "Copy HTML"); this collapses them
// into ONE menu button so the row reads cleanly. It's the same WAI-ARIA
// menu-button pattern as UserMenu.jsx (aria-haspopup/-expanded, role="menu"/
// "menuitem", arrow-key roving with wrap, Home/End, Escape-closes-and-restores-
// focus, click-outside to dismiss) — browser truth pinned in
// frontend/e2e/chat-interactions.spec.js.
//
// The copy LOGIC stays in Chat.jsx: onCopyMarkdown/onCopyHtml just call its
// existing doCopy(); `copied` flips the trigger glyph to a checkmark briefly so
// the user gets the same "Copied!" acknowledgement the text buttons gave.
export default function CopyMenu({ onCopyMarkdown, onCopyHtml, copied = false }) {
  const [open, setOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(0);
  const wrapRef = useRef(null);
  const triggerRef = useRef(null);
  const itemRefs = useRef([]);
  const menuId = useId();

  const items = [
    { key: "md", label: "Copy Markdown", onSelect: onCopyMarkdown },
    { key: "html", label: "Copy rich HTML", onSelect: onCopyHtml },
  ];

  // Move focus to the active item while the menu is open.
  useEffect(() => {
    if (open) itemRefs.current[activeIndex]?.focus();
  }, [open, activeIndex]);

  // Click outside closes (mousedown so it beats an item click).
  useEffect(() => {
    if (!open) return undefined;
    const onDown = (e) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target)) setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);

  function openMenu(index) {
    setActiveIndex(index);
    setOpen(true);
  }

  function close(restoreFocus = true) {
    setOpen(false);
    if (restoreFocus) triggerRef.current?.focus();
  }

  function onTriggerKeyDown(e) {
    if (e.key === "ArrowDown" || e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      openMenu(0);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      openMenu(items.length - 1);
    }
  }

  function onMenuKeyDown(e) {
    const last = items.length - 1;
    switch (e.key) {
      case "ArrowDown":
        e.preventDefault();
        setActiveIndex((i) => (i >= last ? 0 : i + 1));
        break;
      case "ArrowUp":
        e.preventDefault();
        setActiveIndex((i) => (i <= 0 ? last : i - 1));
        break;
      case "Home":
        e.preventDefault();
        setActiveIndex(0);
        break;
      case "End":
        e.preventDefault();
        setActiveIndex(last);
        break;
      case "Escape":
        e.stopPropagation();
        close();
        break;
      case "Tab":
        close(false);
        break;
      default:
        break;
    }
  }

  function activate(item) {
    // Close first (restoring focus to the trigger synchronously), then copy.
    close(true);
    item.onSelect?.();
  }

  return (
    <div className="copy-menu" ref={wrapRef}>
      <button
        type="button"
        className="link copy-menu-trigger"
        ref={triggerRef}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        onClick={() => (open ? close() : openMenu(0))}
        onKeyDown={onTriggerKeyDown}
      >
        {copied ? <IconCheck size={15} /> : <IconCopy size={15} />}
        <span>{copied ? "Copied!" : "Copy"}</span>
        <IconChevronDown size={14} />
      </button>
      {open && (
        <div
          className="copy-menu-panel"
          id={menuId}
          role="menu"
          aria-label="Copy answer"
          onKeyDown={onMenuKeyDown}
        >
          {items.map((item, i) => (
            <button
              key={item.key}
              type="button"
              role="menuitem"
              className="copy-menu-item"
              ref={(el) => { itemRefs.current[i] = el; }}
              tabIndex={i === activeIndex ? 0 : -1}
              onClick={() => activate(item)}
            >
              <IconCopy size={15} />
              <span>{item.label}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
