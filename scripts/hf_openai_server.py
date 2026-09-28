#!/usr/bin/env python3
"""Minimal OpenAI-compatible completions server using Hugging Face Transformers."""

from __future__ import annotations

import argparse
import json
import queue
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Generator, Iterable, List

import torch
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from transformers import AutoModelForCausalLM, AutoTokenizer, TextIteratorStreamer
import uvicorn


@dataclass
class ModelBundle:
    model_id: str
    model: Any
    tokenizer: Any
    device: str


class CompletionRequest(BaseModel):
    model: str
    prompt: str
    max_tokens: int = Field(default=64, ge=1, le=2048)
    temperature: float = Field(default=0.0, ge=0.0)
    top_p: float = Field(default=1.0, gt=0.0, le=1.0)
    stream: bool = False
    stream_options: Dict[str, Any] | None = None


def count_tokens(text: str) -> int:
    return len(text.strip().split()) if text.strip() else 0


def create_app(bundle: ModelBundle) -> FastAPI:
    app = FastAPI(title="Kairos HF OpenAI Compatibility Server")

    @app.get("/healthz")
    def healthz() -> Dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/models")
    def list_models() -> Dict[str, Any]:
        return {
            "object": "list",
            "data": [
                {
                    "id": bundle.model_id,
                    "object": "model",
                    "owned_by": "kairos-local",
                }
            ],
        }

    def generate_text(req: CompletionRequest) -> str:
        if req.model != bundle.model_id:
            raise HTTPException(status_code=400, detail=f"Unknown model id: {req.model}")

        inputs = bundle.tokenizer(req.prompt, return_tensors="pt")
        input_ids = inputs["input_ids"].to(bundle.device)
        attention_mask = inputs.get("attention_mask")
        if attention_mask is not None:
            attention_mask = attention_mask.to(bundle.device)

        do_sample = req.temperature > 0.0
        generation_kwargs = {
            "input_ids": input_ids,
            "attention_mask": attention_mask,
            "max_new_tokens": req.max_tokens,
            "do_sample": do_sample,
            "temperature": max(req.temperature, 1e-5) if do_sample else 1.0,
            "top_p": req.top_p,
            "pad_token_id": bundle.tokenizer.eos_token_id,
            "eos_token_id": bundle.tokenizer.eos_token_id,
        }

        with torch.inference_mode():
            output_ids = bundle.model.generate(**generation_kwargs)
        completion_ids = output_ids[0][input_ids.shape[1] :]
        return bundle.tokenizer.decode(completion_ids, skip_special_tokens=True)

    def stream_text(req: CompletionRequest) -> Iterable[str]:
        if req.model != bundle.model_id:
            raise HTTPException(status_code=400, detail=f"Unknown model id: {req.model}")

        inputs = bundle.tokenizer(req.prompt, return_tensors="pt")
        input_ids = inputs["input_ids"].to(bundle.device)
        attention_mask = inputs.get("attention_mask")
        if attention_mask is not None:
            attention_mask = attention_mask.to(bundle.device)

        streamer = TextIteratorStreamer(
            bundle.tokenizer,
            skip_prompt=True,
            skip_special_tokens=True,
            timeout=60.0,
        )

        do_sample = req.temperature > 0.0
        generation_kwargs = {
            "input_ids": input_ids,
            "attention_mask": attention_mask,
            "max_new_tokens": req.max_tokens,
            "do_sample": do_sample,
            "temperature": max(req.temperature, 1e-5) if do_sample else 1.0,
            "top_p": req.top_p,
            "pad_token_id": bundle.tokenizer.eos_token_id,
            "eos_token_id": bundle.tokenizer.eos_token_id,
            "streamer": streamer,
        }

        worker_error: "queue.Queue[Exception]" = queue.Queue()

        def _worker() -> None:
            try:
                with torch.inference_mode():
                    bundle.model.generate(**generation_kwargs)
            except Exception as exc:  # pragma: no cover
                worker_error.put(exc)

        thread = threading.Thread(target=_worker, daemon=True)
        thread.start()

        completion_text_parts: List[str] = []
        for text_piece in streamer:
            completion_text_parts.append(text_piece)
            event = {
                "id": f"cmpl-{uuid.uuid4().hex}",
                "object": "text_completion",
                "created": int(time.time()),
                "model": bundle.model_id,
                "choices": [
                    {
                        "index": 0,
                        "text": text_piece,
                        "finish_reason": None,
                    }
                ],
            }
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

        thread.join(timeout=1.0)
        if not worker_error.empty():
            raise HTTPException(status_code=500, detail=str(worker_error.get()))

        completion_text = "".join(completion_text_parts)
        usage = {
            "prompt_tokens": count_tokens(req.prompt),
            "completion_tokens": max(count_tokens(completion_text), 1),
            "total_tokens": count_tokens(req.prompt) + max(count_tokens(completion_text), 1),
        }
        final_event = {
            "id": f"cmpl-{uuid.uuid4().hex}",
            "object": "text_completion",
            "created": int(time.time()),
            "model": bundle.model_id,
            "choices": [{"index": 0, "text": "", "finish_reason": "stop"}],
            "usage": usage,
        }
        yield f"data: {json.dumps(final_event, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    @app.post("/v1/completions")
    def completions(req: CompletionRequest):
        if req.stream:
            return StreamingResponse(stream_text(req), media_type="text/event-stream")

        completion_text = generate_text(req)
        usage = {
            "prompt_tokens": count_tokens(req.prompt),
            "completion_tokens": max(count_tokens(completion_text), 1),
            "total_tokens": count_tokens(req.prompt) + max(count_tokens(completion_text), 1),
        }
        response = {
            "id": f"cmpl-{uuid.uuid4().hex}",
            "object": "text_completion",
            "created": int(time.time()),
            "model": bundle.model_id,
            "choices": [{"index": 0, "text": completion_text, "finish_reason": "stop"}],
            "usage": usage,
        }
        return JSONResponse(content=response)

    return app


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="HF OpenAI-compatible server for Kairos baseline.")
    parser.add_argument("--model-id", required=True, help="Hugging Face model id")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--dtype", choices=["float32", "bfloat16", "float16"], default="float32")
    parser.add_argument("--device", choices=["cpu", "cuda", "auto"], default="auto")
    return parser.parse_args()


def resolve_dtype(dtype: str):
    if dtype == "bfloat16":
        return torch.bfloat16
    if dtype == "float16":
        return torch.float16
    return torch.float32


def main() -> None:
    args = parse_args()

    if args.device == "auto":
        device = "cuda" if torch.cuda.is_available() else "cpu"
    else:
        device = args.device
        if device == "cuda" and not torch.cuda.is_available():
            raise RuntimeError("CUDA requested but no CUDA device is available.")

    dtype = resolve_dtype(args.dtype)

    tokenizer = AutoTokenizer.from_pretrained(args.model_id)
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token

    model = AutoModelForCausalLM.from_pretrained(
        args.model_id,
        torch_dtype=dtype,
        low_cpu_mem_usage=True,
    )
    model.to(device)
    model.eval()

    app = create_app(
        ModelBundle(
            model_id=args.model_id,
            model=model,
            tokenizer=tokenizer,
            device=device,
        )
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()

