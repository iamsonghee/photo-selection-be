"""셀프 고객 품질 판정 비용 실험 — 같은 사진을 설정만 바꿔 판정하고 토큰·일치율을 비교한다. DB에는 쓰지 않는다.

    .venv/bin/python scripts/quality_cost_experiment.py <project_id> [--sample 150] [--out result.json]
        [--only current,think_min_flex] [--flex-sample 150] [--timeout 900]

지연 분포(p50·p95·최대)와 시도별 응답 상태(429·5xx·timeout)를 함께 남긴다 — Flex 타임아웃·재시도 기준을 정할 때 쓴다.

변형: 지금 설정(1200px 미리보기, thinking 기본) / thinking MINIMAL / + 해상도 MEDIUM / + LOW / 300px 썸네일 /
Flex(일부만 — 받아주는지·지연 확인). 기준은 "지금 설정" 재실행이고, DB에 저장된 판정(같은 설정의 이전 실행)과
지금 설정의 일치율은 설정을 안 바꿔도 생기는 흔들림(노이즈)이다 — 변형 차이를 이것과 견줘 본다.
SDK 대신 REST로 부른다(로컬 Python 3.9용 SDK 1.47에는 thinking_level·service_tier가 없음).
"""
import argparse
import asyncio
import base64
import json
import random
import sys
import time
from collections import Counter
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import (GEMINI_API_KEY, GEMINI_FLASH_INPUT_PRICE_PER_1M,  # noqa: E402
                        GEMINI_FLASH_MODEL, GEMINI_FLASH_OUTPUT_PRICE_PER_1M)
from app.customer_ai import CUSTOMER_QUALITY_PROMPT_VERSION, _all_rows  # noqa: E402
from app.db import get_supabase  # noqa: E402
from app.downloader import download_all  # noqa: E402
from app.gemini_quality_client import _CUSTOMER_PROMPT, CustomerPhotoAssessment  # noqa: E402

URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_FLASH_MODEL}:generateContent"
MINIMAL = {"thinkingConfig": {"thinkingLevel": "MINIMAL"}}
VARIANTS = {  # 이름: (이미지, generationConfig 추가값, 요청 추가값)
    "current": ("preview", {}, {}),
    "think_min": ("preview", MINIMAL, {}),
    "think_min_medium": ("preview", {**MINIMAL, "mediaResolution": "MEDIA_RESOLUTION_MEDIUM"}, {}),
    "think_min_low": ("preview", {**MINIMAL, "mediaResolution": "MEDIA_RESOLUTION_LOW"}, {}),
    "think_min_thumb300": ("thumb", MINIMAL, {}),
    "think_min_flex": ("preview", MINIMAL, {"serviceTier": "flex"}),
    "flex": ("preview", {}, {"serviceTier": "flex"}),  # 운영 설정 그대로(thinking 지정 없음) + Flex
}


def _schema() -> dict:
    """pydantic 스키마의 $ref(enum)를 펼친다 — responseSchema는 $ref를 받지 않는다."""
    schema = CustomerPhotoAssessment.model_json_schema()
    defs = schema.pop("$defs", {})

    def inline(node):
        if isinstance(node, dict):
            if "$ref" in node:
                return inline(defs[node["$ref"].split("/")[-1]])
            return {key: inline(value) for key, value in node.items() if key not in ("title", "default")}
        if isinstance(node, list):
            return [inline(item) for item in node]
        return node

    schema = inline(schema)
    for prop in schema["properties"].values():  # Optional[str] → anyOf[str, null]
        if "anyOf" in prop:
            prop.clear()
            prop.update({"type": "string", "nullable": True})
    return schema


async def _call(client, image: bytes, generation: dict, extra: dict, timeout: float) -> dict:
    body = {
        "contents": [{"parts": [{"text": _CUSTOMER_PROMPT},
                                {"inlineData": {"mimeType": "image/jpeg", "data": base64.b64encode(image).decode()}}]}],
        "generationConfig": {"responseMimeType": "application/json", "responseSchema": _schema(), "temperature": 0, **generation},
        **extra,
    }
    started = time.perf_counter()
    statuses: list = []  # 시도마다 상태 코드(또는 "timeout") — 첫 시도 지연과 재시도 원인을 본다
    response = None
    for attempt in range(3):
        try:
            response = await client.post(URL, params={"key": GEMINI_API_KEY}, json=body, timeout=timeout)
        except httpx.TimeoutException:
            statuses.append("timeout")
            response = None
            break
        statuses.append(response.status_code)
        if response.status_code in (429, 500, 503) and attempt < 2:
            retry_after = response.headers.get("retry-after")
            await asyncio.sleep(float(retry_after) if retry_after and retry_after.isdigit() else 2 ** attempt * 2)
            continue
        break
    seconds = time.perf_counter() - started
    if response is None or response.status_code != 200:
        detail = "timeout" if response is None else f"{response.status_code} {response.text[:200]}"
        return {"error": detail, "seconds": seconds, "statuses": statuses}
    data = response.json()
    usage = data.get("usageMetadata", {})
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    return {"result": json.loads(text), "seconds": seconds, "statuses": statuses, "prompt": usage.get("promptTokenCount", 0),
            "output": usage.get("candidatesTokenCount", 0), "thinking": usage.get("thoughtsTokenCount", 0)}


def _percentiles(values: list[float]) -> list[float]:
    ordered = sorted(values) or [0.0]
    pick = lambda q: ordered[min(len(ordered) - 1, int(q * len(ordered)))]  # noqa: E731
    return [round(pick(0.5), 1), round(pick(0.95), 1), round(ordered[-1], 1)]


def _flags(result: dict) -> dict:
    """화면이 실제로 쓰는 해석(FE customer-select-server.ts toPhoto)."""
    return {"blur": result.get("blur_or_shake") in ("possible", "likely") or result.get("focus_issue") in ("possible", "likely"),
            "eyes": result.get("eyes_closed") == "likely", "people": result.get("people")}


def _agreement(a: dict, b: dict, ids: list[str]) -> dict:
    both = [i for i in ids if i in a and i in b]
    if not both:
        return {}
    out = {"n": len(both)}
    for key in ("blur", "eyes", "people"):
        out[key] = round(sum(_flags(a[i])[key] == _flags(b[i])[key] for i in both) / len(both), 3)
    out["blur_flagged"] = [sum(_flags(x[i])["blur"] for i in both) for x in (a, b)]
    out["eyes_flagged"] = [sum(_flags(x[i])["eyes"] for i in both) for x in (a, b)]
    return out


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("project_id")
    parser.add_argument("--sample", type=int, default=150)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--out", default="quality_cost_experiment.json")
    parser.add_argument("--only", default="", help="쉼표로 구분한 변형 이름(current는 비교 기준이라 항상 포함)")
    parser.add_argument("--flex-sample", type=int, default=20, help="Flex 변형만 이 장수로(대기·비용 절약)")
    parser.add_argument("--timeout", type=float, default=900, help="요청 하나 제한시간(초) — 지연 분포를 보려면 길게")
    args = parser.parse_args()

    db = get_supabase()
    photos = _all_rows(lambda: db.table("customer_photos").select("id,preview_url,thumb_url")
                       .eq("project_id", args.project_id).order("id"))
    stored = {row["photo_id"]: row["raw_response"] for row in _all_rows(
        lambda: db.table("customer_quality_assessments").select("photo_id,raw_response")
        .eq("project_id", args.project_id).eq("prompt_version", CUSTOMER_QUALITY_PROMPT_VERSION).order("photo_id"))}
    sample = random.Random(args.seed).sample(photos, min(args.sample, len(photos)))
    ids = [photo["id"] for photo in sample]
    images = {"preview": await download_all([p["preview_url"] for p in sample]),
              "thumb": await download_all([p["thumb_url"] for p in sample])}

    results: dict[str, dict] = {}
    sem = asyncio.Semaphore(4)
    chosen = {"current", *filter(None, args.only.split(","))} if args.only else set(VARIANTS)
    async with httpx.AsyncClient() as client:
        for name, (image_kind, generation, extra) in VARIANTS.items():
            if name not in chosen:
                continue
            count = min(args.flex_sample, len(sample)) if "flex" in name else len(sample)

            async def one(index):
                image = images[image_kind][index]
                if image is None:
                    return ids[index], {"error": "download failed"}
                async with sem:
                    return ids[index], await _call(client, image, generation, extra, args.timeout)

            results[name] = dict(await asyncio.gather(*[one(i) for i in range(count)]))
            print(f"{name}: done", file=sys.stderr)

    judged = {name: {i: r["result"] for i, r in rows.items() if "result" in r} for name, rows in results.items()}
    summary = {}
    for name, rows in results.items():
        ok = [r for r in rows.values() if "result" in r]
        avg = lambda key: round(sum(r[key] for r in ok) / max(1, len(ok)), 1)  # noqa: E731
        flex = 0.5 if "flex" in name else 1.0
        per_photo = (avg("prompt") * GEMINI_FLASH_INPUT_PRICE_PER_1M + (avg("output") + avg("thinking")) * GEMINI_FLASH_OUTPUT_PRICE_PER_1M) / 1e6 * flex
        summary[name] = {
            "ok": len(ok), "errors": len(rows) - len(ok),
            "avg_prompt_tokens": avg("prompt"), "avg_output_tokens": avg("output"), "avg_thinking_tokens": avg("thinking"),
            "avg_seconds": avg("seconds"), "usd_per_1000_photos": round(per_photo * 1000, 3),
            "seconds_p50_p95_max": _percentiles([r["seconds"] for r in rows.values()]),
            "statuses": dict(Counter(str(status) for r in rows.values() for status in r.get("statuses", []))),
            "vs_current": _agreement(judged[name], judged["current"], ids) if name != "current" else None,
        }
    summary["noise_current_vs_stored"] = _agreement(judged["current"], stored, ids)
    Path(args.out).write_text(json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=1))
    print(json.dumps(summary, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    asyncio.run(main())
