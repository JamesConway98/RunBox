"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { Badge, Card, CardBody, Skeleton } from "@/components/ui/primitives";
import { batchesApi } from "@/lib/api";
import type { Batch } from "@/lib/batchTypes";

export default function EvalsIndexPage() {
  const [batches, setBatches] = useState<Batch[] | null>(null);

  useEffect(() => {
    void batchesApi
      .list()
      .then((page) => setBatches(page.data))
      .catch(() => setBatches([]));
  }, []);

  return (
    <div className="space-y-5">
      <header>
        <h1 className="text-xl font-semibold tracking-tight">Evals</h1>
        <p className="text-sm text-muted">
          Score a batch, compare models, see pass rate against cost.
        </p>
      </header>

      {batches === null ? (
        <Skeleton className="h-32 rounded-xl" />
      ) : batches.length === 0 ? (
        <Card className="px-6 py-12 text-center">
          <p className="text-sm font-medium">Nothing to evaluate yet</p>
          <p className="mx-auto mt-1 max-w-sm text-sm text-muted">
            Evals score the runs in a batch. Upload a dataset with an{" "}
            <code>expected</code> column and launch one first.
          </p>
          <Button variant="primary" size="sm" className="mt-4" asChild>
            <Link href="/datasets">Go to datasets</Link>
          </Button>
        </Card>
      ) : (
        <div className="space-y-2">
          {batches.map((batch) => (
            <Link key={batch.id} href={`/evals/${batch.id}`}>
              <Card className="transition-colors hover:border-accent/50">
                <CardBody className="flex flex-wrap items-center gap-3">
                  <Badge tone={batch.status === "completed" ? "success" : "accent"}>
                    {batch.status}
                  </Badge>
                  <span className="font-medium">{batch.name}</span>
                  <span className="font-mono text-xs text-subtle">
                    {batch.total_runs.toLocaleString()} runs
                  </span>
                  <span className="ml-auto text-xs text-subtle">
                    {new Date(batch.created_at).toLocaleDateString()}
                  </span>
                </CardBody>
              </Card>
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}
