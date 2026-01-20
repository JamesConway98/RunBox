"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { Badge, Card, CardBody, Input, Label, Skeleton, Textarea } from "@/components/ui/primitives";
import { track } from "@/lib/analytics";
import { ApiError, batchesApi, datasetsApi } from "@/lib/api";
import type { Batch, Dataset } from "@/lib/batchTypes";
import { MODELS } from "@/lib/models";

export default function DatasetsPage() {
  const [datasets, setDatasets] = useState<Dataset[]>([]);
  const [batches, setBatches] = useState<Batch[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [d, b] = await Promise.all([datasetsApi.list(), batchesApi.list()]);
      setDatasets(d.data);
      setBatches(b.data);
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not load datasets.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-xl font-semibold tracking-tight">Datasets</h1>
        <p className="text-sm text-muted">
          Upload test cases, then fan them out across models as a batch.
        </p>
      </header>

      {error && (
        <p className="rounded-lg border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger">
          {error}
        </p>
      )}

      <div className="grid gap-6 lg:grid-cols-2">
        <UploadCard onUploaded={refresh} />
        <LaunchCard datasets={datasets} onLaunched={refresh} />
      </div>

      <section className="space-y-3">
        <h2 className="text-sm font-medium">Datasets</h2>
        {loading ? (
          <Skeleton className="h-24 rounded-xl" />
        ) : datasets.length === 0 ? (
          <Card className="px-6 py-10 text-center">
            <p className="text-sm font-medium">No datasets yet</p>
            <p className="mt-1 text-sm text-muted">
              Upload a .jsonl or .csv with an <code>input</code> column to get started.
            </p>
          </Card>
        ) : (
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {datasets.map((dataset) => (
              <Card key={dataset.id}>
                <CardBody className="space-y-1">
                  <div className="flex items-start justify-between gap-2">
                    <p className="font-medium">{dataset.name}</p>
                    <Badge>{dataset.case_count.toLocaleString()} cases</Badge>
                  </div>
                  {dataset.description && (
                    <p className="text-xs text-muted">{dataset.description}</p>
                  )}
                  <p className="font-mono text-[11px] text-subtle">
                    {new Date(dataset.created_at).toLocaleDateString()}
                  </p>
                </CardBody>
              </Card>
            ))}
          </div>
        )}
      </section>

      <section className="space-y-3">
        <h2 className="text-sm font-medium">Batches</h2>
        {loading ? (
          <Skeleton className="h-24 rounded-xl" />
        ) : batches.length === 0 ? (
          <Card className="px-6 py-10 text-center text-sm text-muted">
            No batches yet. Launch one above.
          </Card>
        ) : (
          <div className="space-y-2">
            {batches.map((batch) => (
              <Link key={batch.id} href={`/evals/${batch.id}`}>
                <Card className="transition-colors hover:border-accent/50">
                  <CardBody className="flex flex-wrap items-center gap-3">
                    <Badge
                      tone={
                        batch.status === "completed"
                          ? "success"
                          : batch.status === "cancelled"
                            ? "warning"
                            : "accent"
                      }
                    >
                      {batch.status}
                    </Badge>
                    <span className="font-medium">{batch.name}</span>
                    <span className="font-mono text-xs text-subtle">
                      {batch.total_runs.toLocaleString()} runs · {batch.models.length} models
                    </span>
                    <span className="ml-auto text-xs text-subtle">
                      {new Date(batch.created_at).toLocaleString()}
                    </span>
                  </CardBody>
                </Card>
              </Link>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}

function UploadCard({ onUploaded }: { onUploaded: () => void }) {
  const [file, setFile] = useState<File | null>(null);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  const submit = useCallback(async () => {
    if (!file) return;
    setBusy(true);
    setError(null);
    try {
      const dataset = await datasetsApi.upload(file, name.trim());
      track("dataset_uploaded", {
        case_count: dataset.case_count,
        file_type: file.name.split(".").pop() ?? "unknown",
        size_bytes: file.size,
      });
      setFile(null);
      setName("");
      if (inputRef.current) inputRef.current.value = "";
      onUploaded();
    } catch (err) {
      // The server names the offending line number; surfacing it verbatim is
      // far more useful than "upload failed".
      setError(err instanceof ApiError ? err.message : "Upload failed.");
    } finally {
      setBusy(false);
    }
  }, [file, name, onUploaded]);

  return (
    <Card>
      <CardBody className="space-y-3">
        <h2 className="text-sm font-medium">Upload a dataset</h2>

        <div
          onDragOver={(e) => {
            e.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={(e) => {
            e.preventDefault();
            setDragging(false);
            const dropped = e.dataTransfer.files[0];
            if (dropped) setFile(dropped);
          }}
          className={`rounded-lg border border-dashed px-4 py-8 text-center transition-colors ${
            dragging ? "border-accent bg-accent/5" : "border-border"
          }`}
        >
          <p className="text-sm">{file ? file.name : "Drop a .jsonl or .csv here"}</p>
          <p className="mt-1 text-xs text-subtle">
            Needs an <code>input</code> column. <code>expected</code> enables scoring.
          </p>
          <input
            ref={inputRef}
            type="file"
            accept=".jsonl,.ndjson,.csv"
            onChange={(e) => setFile(e.target.files?.[0] ?? null)}
            className="mt-3 text-xs text-muted file:mr-2 file:rounded file:border-0
                       file:bg-raised file:px-2 file:py-1 file:text-xs"
          />
        </div>

        <div className="space-y-1">
          <Label htmlFor="dataset-name">Name (optional)</Label>
          <Input
            id="dataset-name"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder={file?.name ?? "My test cases"}
          />
        </div>

        {error && <p className="text-xs text-danger">{error}</p>}

        <Button variant="primary" onClick={() => void submit()} disabled={!file || busy}>
          {busy ? "Uploading…" : "Upload"}
        </Button>
      </CardBody>
    </Card>
  );
}

function LaunchCard({
  datasets,
  onLaunched,
}: {
  datasets: Dataset[];
  onLaunched: () => void;
}) {
  const [datasetId, setDatasetId] = useState("");
  const [name, setName] = useState("");
  const [template, setTemplate] = useState("{{input}}");
  const [models, setModels] = useState<string[]>([MODELS[0]?.id ?? ""]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const dataset = datasets.find((d) => d.id === datasetId);
  const runCount = (dataset?.case_count ?? 0) * models.length;

  const submit = useCallback(async () => {
    if (!datasetId || models.length === 0) return;
    setBusy(true);
    setError(null);
    try {
      track("batch_launched", {
        model_count: models.length,
        case_count: dataset?.case_count ?? 0,
        run_count: runCount,
      });
      await batchesApi.create({
        dataset_id: datasetId,
        name: name.trim() || `${dataset?.name ?? "Batch"} · ${models.length} models`,
        prompt_template: template,
        models,
      });
      setName("");
      onLaunched();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not launch the batch.");
    } finally {
      setBusy(false);
    }
  }, [datasetId, models, name, template, dataset, onLaunched]);

  return (
    <Card>
      <CardBody className="space-y-3">
        <h2 className="text-sm font-medium">Launch a batch</h2>

        <div className="space-y-1">
          <Label htmlFor="batch-dataset">Dataset</Label>
          <select
            id="batch-dataset"
            value={datasetId}
            onChange={(e) => setDatasetId(e.target.value)}
            className="w-full rounded-lg border border-border bg-bg px-2.5 py-1.5 text-sm"
          >
            <option value="">Select a dataset…</option>
            {datasets.map((d) => (
              <option key={d.id} value={d.id}>
                {d.name} ({d.case_count.toLocaleString()} cases)
              </option>
            ))}
          </select>
        </div>

        <div className="space-y-1">
          <Label htmlFor="batch-template">Prompt template</Label>
          <Textarea
            id="batch-template"
            value={template}
            onChange={(e) => setTemplate(e.target.value)}
            rows={2}
            className="font-mono text-xs"
          />
          <p className="text-[11px] text-subtle">
            <code>{"{{input}}"}</code> is replaced with each case.
          </p>
        </div>

        <fieldset className="space-y-1">
          <legend className="text-xs font-medium text-muted">Models</legend>
          <div className="flex flex-wrap gap-1.5">
            {MODELS.map((model) => {
              const on = models.includes(model.id);
              return (
                <button
                  key={model.id}
                  type="button"
                  aria-pressed={on}
                  onClick={() =>
                    setModels((current) =>
                      on ? current.filter((m) => m !== model.id) : [...current, model.id],
                    )
                  }
                  className={`rounded-md border px-2 py-1 text-xs transition-colors ${
                    on
                      ? "border-accent bg-accent/10 text-accent"
                      : "border-border text-muted hover:bg-raised"
                  }`}
                >
                  {model.name}
                </button>
              );
            })}
          </div>
        </fieldset>

        {/* Told before launching, not after. A batch of 9,000 runs is a real
            amount of money and the number should not be a surprise. */}
        {runCount > 0 && (
          <p className="text-xs text-muted">
            This will create <span className="font-medium text-fg">{runCount.toLocaleString()}</span>{" "}
            runs.
          </p>
        )}

        {error && <p className="text-xs text-danger">{error}</p>}

        <Button
          variant="primary"
          onClick={() => void submit()}
          disabled={!datasetId || models.length === 0 || busy}
        >
          {busy ? "Launching…" : "Launch batch"}
        </Button>
      </CardBody>
    </Card>
  );
}
