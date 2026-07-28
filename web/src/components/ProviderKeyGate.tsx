"use client";

import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Badge, Card, CardBody, Input } from "@/components/ui/primitives";
import { useProviderKey } from "@/lib/useProviderKey";

/**
 * Collects the visitor's own model provider key.
 *
 * Runbox holds no provider key, so this is the thing that makes anything run.
 * The copy says where the key goes and where it does not, because "we never
 * store your key" is a claim someone is entitled to have explained rather than
 * asserted.
 */
export function ProviderKeyGate({ compact = false }: { compact?: boolean }) {
  const { key, ready, save, clear, masked } = useProviderKey();
  const [draft, setDraft] = useState("");
  const [editing, setEditing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Avoids a flash of the empty state before localStorage has been read.
  if (!ready) return null;

  const submit = () => {
    const value = draft.trim();
    if (!value) return;
    // The same shapes the server accepts, checked here so the feedback is
    // immediate rather than a round trip away.
    if (value.startsWith("rb_live_")) {
      setError("That is a Runbox API key. The provider key starts with 'sk-'.");
      return;
    }
    if (!/^sk-/.test(value)) {
      setError("Expected a key starting 'sk-ant-' (Anthropic) or 'sk-' (OpenAI).");
      return;
    }
    save(value);
    setDraft("");
    setEditing(false);
    setError(null);
  };

  if (key && !editing) {
    return (
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <Badge tone="success">key set</Badge>
        <code className="font-mono text-subtle">{masked}</code>
        <button
          type="button"
          onClick={() => setEditing(true)}
          className="text-muted underline-offset-2 hover:text-fg hover:underline"
        >
          change
        </button>
        <button
          type="button"
          onClick={clear}
          className="text-muted underline-offset-2 hover:text-danger hover:underline"
        >
          remove
        </button>
      </div>
    );
  }

  return (
    <Card className={compact ? "" : "border-accent/40"}>
      <CardBody className="space-y-3">
        <div>
          <h2 className="text-sm font-medium">Your model provider key</h2>
          <p className="mt-1 text-xs text-muted">
            Runbox does not supply one — runs execute on your own key, so you see
            exactly what they cost on your provider bill.
          </p>
        </div>

        <div className="flex flex-wrap gap-2">
          <Input
            type="password"
            value={draft}
            onChange={(e) => {
              setDraft(e.target.value);
              setError(null);
            }}
            onKeyDown={(e) => e.key === "Enter" && submit()}
            placeholder="sk-ant-…"
            autoComplete="off"
            spellCheck={false}
            aria-label="Model provider API key"
            className="min-w-0 flex-1 font-mono text-xs"
          />
          <Button variant="primary" onClick={submit} disabled={!draft.trim()}>
            Save
          </Button>
          {key && (
            <Button variant="ghost" onClick={() => setEditing(false)}>
              Cancel
            </Button>
          )}
        </div>

        {error && <p className="text-xs text-danger">{error}</p>}

        <details className="text-xs text-subtle">
          <summary className="cursor-pointer hover:text-muted">
            Where does this key go?
          </summary>
          <ul className="mt-2 space-y-1 pl-4">
            <li className="list-disc">
              Stored in this browser only, and sent as a header when you start a run.
            </li>
            <li className="list-disc">
              Held in Redis for the seconds between starting a run and a worker
              claiming it, then deleted on read. Never written to the database.
            </li>
            <li className="list-disc">
              Passed to the sandbox&apos;s proxy, not into the sandbox. The container
              that runs the agent never sees it.
            </li>
            <li className="list-disc">
              It does transit the server, so an operator with production access
              could capture it. That is true of every hosted BYOK product; use a
              key you can rotate.
            </li>
          </ul>
        </details>
      </CardBody>
    </Card>
  );
}
