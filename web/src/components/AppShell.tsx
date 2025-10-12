"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { Moon, Sun } from "@/components/ui/primitives";
import { cn } from "@/lib/cn";

const NAV = [
  { href: "/", label: "Playground" },
  { href: "/runs", label: "Runs" },
  { href: "/models", label: "Models" },
  { href: "/datasets", label: "Datasets" },
  { href: "/evals", label: "Evals" },
  { href: "/usage", label: "Usage" },
] as const;

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();

  return (
    <div className="flex min-h-screen flex-col">
      <header className="sticky top-0 z-40 border-b border-border bg-bg/80 backdrop-blur">
        <div className="mx-auto flex h-14 max-w-[1600px] items-center gap-6 px-4">
          <Link href="/" className="flex items-center gap-2 font-semibold tracking-tight">
            <span className="grid size-6 place-items-center rounded-md bg-accent text-[11px] font-bold text-accent-fg">
              R
            </span>
            Runbox
          </Link>

          {/* Scrollable rather than collapsed into a hamburger. Six items fit
              on a phone if they are allowed to scroll, and a menu that hides
              the primary navigation costs a tap on every single move. */}
          <nav className="-mx-1 flex flex-1 gap-1 overflow-x-auto scrollbar-thin">
            {NAV.map((item) => {
              const active =
                item.href === "/" ? pathname === "/" : pathname.startsWith(item.href);
              return (
                <Link
                  key={item.href}
                  href={item.href}
                  aria-current={active ? "page" : undefined}
                  className={cn(
                    "whitespace-nowrap rounded-lg px-2.5 py-1.5 text-sm transition-colors",
                    active ? "bg-raised font-medium text-fg" : "text-muted hover:text-fg",
                  )}
                >
                  {item.label}
                </Link>
              );
            })}
          </nav>

          <ThemeToggle />
        </div>
      </header>

      <main className="mx-auto w-full max-w-[1600px] flex-1 px-4 py-6">{children}</main>
    </div>
  );
}

function ThemeToggle() {
  // Starts null so the button does not render the wrong icon during hydration.
  // The class itself is applied by the inline script in the layout, before
  // paint; this state only mirrors it.
  const [dark, setDark] = useState<boolean | null>(null);

  useEffect(() => {
    setDark(document.documentElement.classList.contains("dark"));
  }, []);

  const toggle = useCallback(() => {
    const next = !document.documentElement.classList.contains("dark");
    document.documentElement.classList.toggle("dark", next);
    try {
      localStorage.setItem("runbox-theme", next ? "dark" : "light");
    } catch {
      // Private browsing. The toggle still works for this session.
    }
    setDark(next);
  }, []);

  return (
    <Button
      variant="ghost"
      size="icon"
      onClick={toggle}
      aria-label={dark ? "Switch to light theme" : "Switch to dark theme"}
    >
      {dark === null ? <span className="size-4" /> : dark ? <Sun /> : <Moon />}
    </Button>
  );
}
