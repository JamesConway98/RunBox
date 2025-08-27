import type { Metadata } from "next";

import "./globals.css";

export const metadata: Metadata = {
  title: "Runbox",
  description: "Sandboxed execution and observability for LLM agents.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        {/* Applied before paint so a dark-mode user never sees a white flash.
            Inline because a module would load after first paint, which is
            exactly the frame that matters. */}
        <script
          dangerouslySetInnerHTML={{
            __html: `
              try {
                var stored = localStorage.getItem('runbox-theme');
                var dark = stored ? stored === 'dark'
                  : window.matchMedia('(prefers-color-scheme: dark)').matches;
                if (dark) document.documentElement.classList.add('dark');
              } catch (e) {}
            `,
          }}
        />
      </head>
      <body className="min-h-screen bg-bg text-fg">{children}</body>
    </html>
  );
}
