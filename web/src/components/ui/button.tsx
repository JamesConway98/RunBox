import { Slot } from "@radix-ui/react-slot";
import { type VariantProps, cva } from "class-variance-authority";
import * as React from "react";

import { cn } from "@/lib/cn";

// Vendored in the shadcn idiom rather than pulled from the registry.
//
// shadcn is copy-paste-into-your-repo by design, and the registry version ships
// its own token names (--background, --primary) which would sit alongside this
// project's palette as a second, clashing system. One set of tokens that the
// whole app agrees on is worth more than a generated file.
const buttonVariants = cva(
  "inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-lg " +
    "text-sm font-medium transition-colors " +
    "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent " +
    "focus-visible:ring-offset-2 focus-visible:ring-offset-bg " +
    "disabled:pointer-events-none disabled:opacity-40 " +
    "[&_svg]:pointer-events-none [&_svg]:size-4 [&_svg]:shrink-0",
  {
    variants: {
      variant: {
        primary: "bg-accent text-accent-fg hover:opacity-90",
        secondary: "border border-border bg-surface hover:bg-raised",
        ghost: "hover:bg-raised",
        danger: "bg-danger text-white hover:opacity-90",
        // For destructive actions that are reversible — cancelling a run is
        // not the same as deleting one, and the UI should not shout equally.
        subtle: "border border-border text-muted hover:bg-raised hover:text-fg",
      },
      size: {
        sm: "h-8 px-2.5 text-xs",
        md: "h-9 px-4",
        lg: "h-10 px-5",
        icon: "size-9",
      },
    },
    defaultVariants: { variant: "secondary", size: "md" },
  },
);

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  asChild?: boolean;
}

export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, asChild = false, type = "button", ...props }, ref) => {
    const Comp = asChild ? Slot : "button";
    return (
      <Comp
        ref={ref}
        // Buttons inside a form default to type="submit", which is almost never
        // what a dashboard control wants and produces a mystery page reload.
        {...(asChild ? {} : { type })}
        className={cn(buttonVariants({ variant, size }), className)}
        {...props}
      />
    );
  },
);
Button.displayName = "Button";

export { buttonVariants };
