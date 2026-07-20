import React, { useEffect, useId, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { initials } from "./initials.js";
import { formatBadge } from "./attention.js";
import { IconShield, IconInfo, IconSun, IconMoon, IconSignOut } from "./icons.jsx";

// The top-bar account control: a round avatar (initials from the email) that opens
// a menu holding everything that used to sit loose in the bar — Admin (admins
// only), About, the light/dark toggle, and Sign out. Admin attention (the count of
// things needing an admin's attention) surfaces BOTH as a small pill on the avatar
// corner and as a badge on the Admin menu item — Admin no longer has its own top-bar
// link, so the avatar is where "something needs you" has to read at a glance.
//
// It's a real ARIA menu button (WAI-ARIA menu-button pattern): aria-haspopup/-expanded
// on the trigger, role="menu"/"menuitem" items, arrow-key roving with wrap, Home/End,
// Escape-closes-and-restores-focus, and click-outside to dismiss. Browser truth
// (focus, keyboard) is pinned in frontend/e2e/user-menu.spec.js.
export default function UserMenu({
  email,
  isAdmin,
  attentionTotal = 0,
  theme,
  onToggleTheme,
  onSignOut,
  onAbout,
}) {
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(0);
  const wrapRef = useRef(null);
  const triggerRef = useRef(null);
  const itemRefs = useRef([]);
  const menuId = useId();

  // The menu contents (order = visual + roving order). keepOpen items (the theme
  // toggle) don't dismiss on activation, so the user can flip back and forth and
  // watch it switch; everything else navigates/opens-a-modal/leaves, so it closes.
  const badge = isAdmin ? formatBadge(attentionTotal) : "";
  const items = [];
  if (isAdmin) {
    items.push({
      key: "admin",
      label: "Admin",
      Icon: IconShield,
      badge,
      onSelect: () => navigate("/admin"),
    });
  }
  items.push({ key: "about", label: "About IPEDS Oracle", Icon: IconInfo, onSelect: onAbout });
  items.push({
    key: "theme",
    label: `Switch to ${theme === "dark" ? "light" : "dark"} mode`,
    Icon: theme === "dark" ? IconSun : IconMoon,
    onSelect: onToggleTheme,
    keepOpen: true,
  });
  items.push({ key: "signout", label: "Sign out", Icon: IconSignOut, onSelect: onSignOut });

  // Move DOM focus to the active item whenever the menu is open (open, or the
  // roving index changed while open).
  useEffect(() => {
    if (open) itemRefs.current[activeIndex]?.focus();
  }, [open, activeIndex]);

  // Click outside closes (mousedown so it beats the item click).
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
        // Let focus leave naturally, but the menu shouldn't linger behind it.
        close(false);
        break;
      default:
        break;
    }
  }

  function activate(item) {
    if (item.keepOpen) {
      item.onSelect?.();
      return;
    }
    // Close first, restoring focus to the avatar synchronously — so an item that
    // opens a modal (About) leaves the avatar as the active element, which the
    // modal then captures as the control to return focus to on dismiss. (For
    // navigate/sign-out the focus target is moot — the view changes.)
    close(true);
    item.onSelect?.();
  }

  const label = badge
    ? `Account menu, ${attentionTotal} ${attentionTotal === 1 ? "item needs" : "items need"} attention`
    : "Account menu";

  return (
    <div className="user-menu" ref={wrapRef}>
      <button
        type="button"
        className="avatar"
        ref={triggerRef}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        aria-label={label}
        onClick={() => (open ? close() : openMenu(0))}
        onKeyDown={onTriggerKeyDown}
      >
        <span aria-hidden="true">{initials(email)}</span>
        {badge && <span className="avatar-badge" aria-hidden="true">{badge}</span>}
      </button>
      {open && (
        <div
          className="user-menu-panel"
          id={menuId}
          role="menu"
          aria-label="Account"
          onKeyDown={onMenuKeyDown}
        >
          {email && <div className="user-menu-email" role="presentation">{email}</div>}
          {items.map((item, i) => (
            <button
              key={item.key}
              type="button"
              role="menuitem"
              className="user-menu-item"
              ref={(el) => { itemRefs.current[i] = el; }}
              tabIndex={i === activeIndex ? 0 : -1}
              onClick={() => activate(item)}
            >
              <item.Icon size={16} />
              <span className="user-menu-label">{item.label}</span>
              {item.badge && (
                <span className="tab-badge attention" aria-hidden="true">{item.badge}</span>
              )}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
