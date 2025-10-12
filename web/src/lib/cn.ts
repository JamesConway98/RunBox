import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";

/**
 * Merge class names, with later Tailwind utilities beating earlier ones.
 *
 * Plain `clsx` would emit `px-2 px-4` and let CSS source order decide, which
 * means a component's default padding sometimes wins over the caller's
 * override depending on how Tailwind happened to sort the stylesheet.
 * `twMerge` resolves the conflict by intent instead.
 */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}
