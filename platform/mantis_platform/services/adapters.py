"""Training provider adapters. The platform owns lineage; the adapter owns the trainer.

Contract: run(job, dataset_version, base_config) -> {"status": "succeeded"|"failed", "provider_job_ref", "artifact_ref",
                                                      "artifact_hash", "log": [...], "candidate_invoke": {...}, "model": str}
"""
import hashlib
import json

from ..config import settings


class LocalStubAdapter:
    """Produces a deterministic checkpoint artifact from the dataset. No GPU. Exists so the full release loop
    can be exercised end-to-end; the resulting candidate points the builtin demo agent at its improved behavior."""
    name = "local_stub"

    def run(self, job, dv, base):
        payload = json.dumps({"dataset_hash": dv.content_hash, "base": base.id, "method": job.method, "params": job.params}, sort_keys=True)
        h = hashlib.sha256(payload.encode()).hexdigest()
        return {"status": "succeeded", "provider_job_ref": f"local:{h[:12]}", "artifact_ref": f"artifacts/{job.project_id}/{h}.ckpt",
                "artifact_hash": h, "model": f"{base.model}+{job.method}@{h[:8]}",
                "candidate_invoke": {"kind": "builtin_demo", "behavior": "v2"} if base.invoke.get("kind") == "builtin_demo" else base.invoke,
                "log": [f"records={dv.item_count}", f"method={job.method}", "trainer=local_stub (no weights updated)"]}


class OpenAIFineTuneAdapter:
    """Launches a real supervised fine-tune on OpenAI's managed service. Requires OPENAI_API_KEY.
    Protected records are refused unless the project policy allows external training."""
    name = "openai_finetune"

    def run(self, job, dv, base):
        if not settings.openai_api_key:
            return {"status": "failed", "log": ["OPENAI_API_KEY not configured"]}
        if job.method != "sft":
            return {"status": "failed", "log": ["openai_finetune adapter supports method=sft only"]}
        import io, openai
        client = openai.OpenAI(api_key=settings.openai_api_key)
        lines = []
        for it in dv.items:
            if it.get("protected_fields") and not job.params.get("_allow_protected"):
                return {"status": "failed", "log": [f"record {it['id']} is protected; external training not allowed by project policy"]}
            inp = it["input"]; prompt = inp.get("prompt") or inp.get("task") or json.dumps(inp)
            lines.append(json.dumps({"messages": [{"role": "user", "content": str(prompt)}, {"role": "assistant", "content": str(it.get("expected") or "")}]}))
        f = client.files.create(file=("train.jsonl", io.BytesIO("\n".join(lines).encode())), purpose="fine-tune")
        ft = client.fine_tuning.jobs.create(training_file=f.id, model=job.params.get("base_model", "gpt-4o-mini-2024-07-18"))
        h = hashlib.sha256(f"{ft.id}:{dv.content_hash}".encode()).hexdigest()
        return {"status": "submitted", "provider_job_ref": ft.id, "artifact_ref": f"openai:{ft.id}", "artifact_hash": h,
                "model": ft.fine_tuned_model or f"pending:{ft.id}", "candidate_invoke": base.invoke,
                "log": [f"uploaded {len(lines)} records as {f.id}", f"fine-tune job {ft.id} status={ft.status}"]}


ADAPTERS = {a.name: a for a in (LocalStubAdapter(), OpenAIFineTuneAdapter())}
