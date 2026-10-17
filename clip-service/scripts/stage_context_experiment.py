"""웨딩 본식 단계 판정 실험 — 사진 한 장씩 판정하는 지금 방식 대신 앞뒤 맥락을 주고 판정한다. DB에는 쓰지 않는다(읽기만).

    # 예상 비용만(호출 없음, 무료)
    .venv/bin/python scripts/stage_context_experiment.py run <project_id> --mode batch --estimate
    # B: 연속 사진 묶음 판정(창 10장, 앞 2장 겹침) — 유료
    .venv/bin/python scripts/stage_context_experiment.py run <project_id> --mode batch --resolution low --out b_low.json
    # C: 임베딩 구간마다 대표 2장, 전체를 한 호출로 단계 배정 — 유료(수 센트)
    .venv/bin/python scripts/stage_context_experiment.py run <project_id> --mode flow --out c.json
    # 채점: 관리자 검수 정답(customer_scene_labels)과 비교. 결과 파일 없이 부르면 지금 저장된 판정(흔들림 확인의 place)을 채점
    .venv/bin/python scripts/stage_context_experiment.py score <project_id> [b_low.json c.json ...]
    # 정답을 로컬 파일로(파일명 연번 구간 [[단계, 시작, 끝], ...], 예: ../../analysis/scene-truth/navy.json)
    .venv/bin/python scripts/stage_context_experiment.py score <project_id> --truth ../../analysis/scene-truth/navy.json [결과...]

배경·지표: photo-selection-fe/docs/wedding-ceremony-scene-plan.md, 2026-10-15 짱쭌 본식 분석(사진별 판정 86.9%, 축가·행진·신부 입장 약함).
사진 순서는 파일명 연번(`content_order`) — 촬영 순서와 같음을 확인한 샘플 기준. 판단 기준은 FE 카탈로그(`sceneHints`)를 보강한 실험용 v2다.
SDK 대신 REST로 부른다(해상도 mediaResolution을 바로 지정, 기존 quality_cost_experiment.py와 같은 방식).
"""
import argparse
import asyncio
import base64
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path

import httpx
import numpy as np
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
# Supabase 값은 BE .env에 있다(clip-service/.env는 Gemini 키만) — 먼저 읽어 둔다(이미 있는 값은 덮지 않음).
load_dotenv(ROOT.parent / ".env")
sys.path.insert(0, str(ROOT))
from app.config import (GEMINI_API_KEY, GEMINI_FLASH_INPUT_PRICE_PER_1M,  # noqa: E402
                        GEMINI_FLASH_MODEL, GEMINI_FLASH_OUTPUT_PRICE_PER_1M)
from app.customer_ai import _all_rows  # noqa: E402
from app.db import get_supabase  # noqa: E402
from app.downloader import download_all  # noqa: E402
from app.scenes import _label_runs, content_order  # noqa: E402

URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_FLASH_MODEL}:generateContent"
UNKNOWN = "알 수 없음"
# 실험용 단계 판단 기준 v2 — 짱쭌 본식 오판정(키스→예식, 노래 듣는 신부→예식, 인계→예식, 신랑신부만 선 원판→연출)을 반영.
STAGES = {
    "웨딩홀·디테일": "사람 없이 식장·연회장 전경, 꽃장식, 청첩장·방명록·소품을 찍은 사진",
    "메이크업·준비": "메이크업·헤어를 하거나 드레스·턱시도를 입는 준비 과정(대기실 안 화장 수정은 신부대기실)",
    "신부대기실": "신부대기실 소파·꽃장식 배경에서 신부가 가족·하객과 찍은 사진, 대기실 안 화장 수정 포함",
    "로비·하객 맞이": "식 전 로비·식장 입구에서 신랑·혼주가 하객을 맞아 인사·악수하는 장면, 방명록·축의대",
    "신랑신부 연출촬영": "하객 없이 신랑신부 또는 신부 단독이 포즈를 잡은 연출 사진 — 식 전 홀 촬영, 식 후 버진로드·세트 연출",
    "화촉점화·혼주 입장": "양가 어머니가 입장해 단상 앞 촛불을 켜는 장면, 혼주가 하객에게 인사하는 장면",
    "신랑 입장": "사회자 개식 선언, 신랑이 혼자 버진로드를 걸어 입장하거나 단상 앞에서 신부를 기다리는 장면",
    "신부 입장": "신부가 아버지와 버진로드로 입장하는 장면, 아버지가 신랑에게 신부를 인계하며 악수·포옹하는 장면, 인계 뒤 둘이 단상으로 가는 장면",
    "예식": "단상 앞에서 진행되는 서약·반지 교환·성혼선언·주례·덕담, 혼주석 반응, 양가 부모님께 인사·포옹 (축가 무대와 행진은 제외)",
    "축가·축하공연": "노래·연주·춤 같은 축하 무대를 하는 사람, 그리고 그 무대를 지켜보거나 듣는 신랑신부·신부 단독 컷",
    "행진·퇴장": "단상 앞에서 하객을 향해 서서 인사하고 버진로드를 함께 걸어 나가는 장면, 행진 중 키스·포옹 클로즈업, 꽃가루·축포·예도",
    "원판·단체 기념촬영": "포토월·단상 앞 정면에서 찍는 가족·친구·하객 단체 사진과, 원판을 시작하며 신랑신부 둘만 정면으로 선 컷(식 전이든 식 후든)",
    "폐백": "한복 차림, 폐백실 병풍·상차림, 절하기·대추·밤 받기",
    "피로연·연회장": "연회장 식사 테이블, 신랑신부가 테이블을 돌며 하객과 인사·건배하는 장면, 2부 드레스",
}
# v3 — 해군·짱쭌 저해상도 묶음 판정(v2)의 남은 오판정: 하객 없는 빈 홀 버진로드 리허설·식 후 연출 → 행진(짱쭌 48, 해군 137),
# 포토월·꽃장식 앞에서 둘이 선 연출 → 신부대기실(해군 170). 하객 유무와 앉은 신부/둘이 선 포즈를 구분 기준으로 넣는다.
STAGES_V3 = {
    **STAGES,
    "신부대기실": "신부대기실 소파에 신부가 앉아 가족·하객을 맞거나 함께 찍은 사진, 대기실 안 화장 수정 포함 (신랑신부 둘이 서서 포즈를 잡으면 연출촬영)",
    "신랑신부 연출촬영": "하객 없이 신랑신부 둘 또는 신부 단독이 포즈를 잡은 연출 사진 — 포토월·꽃장식 앞에서 둘이 서서 찍는 컷, "
                   "하객 없는 빈 홀·버진로드를 걷거나 단상·계단에 앉아 찍는 리허설 컷(식 전), 식 후 버진로드·세트 연출",
    "행진·퇴장": "예식이 끝나고 하객이 객석에 있는 홀에서 단상 앞 인사 후 버진로드를 함께 걸어 나가는 장면, 행진 중 키스·포옹 클로즈업, "
              "꽃가루·축포·예도 (하객 없는 빈 홀을 걷는 컷은 연출촬영)",
}
HINTS = {"v2": STAGES, "v3": STAGES_V3}
NAMES = list(STAGES)
STAGE_LINES = "\n".join(f"- {name}: {hint}" for name, hint in STAGES.items())
IMAGE_TOKENS = {"default": 1080, "medium": 560, "low": 280}  # default는 2026-09 실측(장당 1,073~1,110), medium·low는 공식 문서 값
RESOLUTION = {"default": None, "medium": "MEDIA_RESOLUTION_MEDIUM", "low": "MEDIA_RESOLUTION_LOW"}

BATCH_PROMPT_T = f"""아래 사진 {{n}}장은 한 결혼식 본식을 시간 순서대로 이어 찍은 사진입니다(사진 1이 가장 먼저).
사진마다 촬영 단계를 아래 목록에서 하나씩 고르세요. 한 장만 보지 말고 앞뒤 사진의 흐름을 함께 보세요 — 예를 들어 노래하는 사람 앞뒤의
신부 단독 컷은 축가를 듣는 장면이고, 버진로드를 걸어 나가는 사진 사이의 키스 클로즈업은 행진입니다. 어느 단계에나 나올 수 있는
하객 얼굴·손·소품 클로즈업은 앞뒤 사진의 단계를 따르세요. 정말 알 수 없을 때만 "{UNKNOWN}".
단계(이름: 판단 기준):
{{stage_lines}}
stages에는 사진 1부터 차례로 정확히 {{n}}개를 적으세요."""

FLOW_PROMPT_T = f"""아래는 한 결혼식 본식 사진 전체를 시간 순서대로 나눈 구간 {{k}}개와 구간마다 대표 사진입니다(구간 0이 가장 먼저).
구간마다 촬영 단계를 아래 목록에서 하나씩 고르세요. 본식은 보통 신랑 입장 → 신부 입장 → 예식(중간에 축가가 끼기도 함) → 행진·퇴장 순서로
진행되고 식 전후에 신부대기실·연출촬영·원판이 옵니다. 진행 순서와 앞뒤 구간을 함께 보고 고르세요. 정말 알 수 없을 때만 "{UNKNOWN}".
단계(이름: 판단 기준):
{{stage_lines}}
stages에는 구간 0부터 차례로 정확히 {{k}}개를 적으세요."""


def _schema(n: int) -> dict:
    """개수 제한(minItems·maxItems)은 20개 이하에서만 — 56처럼 크면 Gemini가 400(INVALID_ARGUMENT)으로 거절한다(2026-10-15).
    그보다 크면 응답 개수를 받은 뒤 확인하고 다르면 다시 묻는다(_call)."""
    stages = {"type": "ARRAY", "items": {"type": "STRING", "enum": [*NAMES, UNKNOWN]}}
    if n <= 20:
        stages.update(minItems=n, maxItems=n)
    return {"type": "OBJECT", "required": ["stages"], "properties": {"stages": stages}}


def load_project(project_id: str):
    """사진(파일명 연번 순서)과 임베딩. 읽기만 한다."""
    db = get_supabase()
    rows = _all_rows(lambda: db.table("customer_photos").select("id,filename,order_index,preview_url,thumb_url,taken_at,taken_at_source")
                     .eq("project_id", project_id).order("id"))
    rows = sorted(content_order([{**row, "taken_at": None} for row in rows]), key=lambda row: row["order_index"])
    emb = {row["photo_id"]: row["embedding"] for row in _all_rows(lambda: db.table("customer_ai_embeddings").select("photo_id,embedding")
           .eq("project_id", project_id).order("photo_id"), page_rows=100)}
    vectors = None
    if all(row["id"] in emb for row in rows):
        vectors = np.asarray([emb[row["id"]] if isinstance(emb[row["id"]], list) else json.loads(emb[row["id"]]) for row in rows], dtype=np.float64)
        vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    return rows, vectors


def embedding_segments(vectors, window: int = 5, threshold: float = 0.90, min_len: int = 4) -> list[tuple[int, int]]:
    """앞뒤 window장 평균의 코사인 유사도가 threshold 아래로 떨어지는 곳에서 자른 잘게 나눈 구간 [(시작, 끝)]."""
    n = len(vectors)
    scores = []
    for i in range(window, n - window + 1):
        before, after = vectors[i - window:i].mean(0), vectors[i:i + window].mean(0)
        scores.append((float(before @ after / np.linalg.norm(before) / np.linalg.norm(after)), i))
    cuts: list[int] = []
    for score, i in sorted(scores):
        if score >= threshold:
            break
        if all(abs(i - cut) >= min_len for cut in (0, *cuts, n)):
            cuts.append(i)
    bounds = [0, *sorted(cuts), n]
    return list(zip(bounds, bounds[1:]))


def batch_windows(n: int, size: int, overlap: int) -> list[tuple[int, int, int]]:
    """[(시작, 끝, 이 창에서 라벨을 쓰는 첫 위치)] — 겹친 앞부분은 맥락으로만 보고 라벨은 앞 창 것을 쓴다."""
    windows, start = [], 0
    while start < n:
        end = min(n, start + size)
        windows.append((start, end, 0 if start == 0 else overlap))
        if end == n:
            break
        start = end - overlap
    return windows


async def _call(client, parts: list, n: int, resolution: str, timeout: float) -> dict:
    generation = {"responseMimeType": "application/json", "responseSchema": _schema(n), "temperature": 0}
    if RESOLUTION[resolution]:
        generation["mediaResolution"] = RESOLUTION[resolution]
    body = {"contents": [{"role": "user", "parts": parts}], "generationConfig": generation}
    last = None
    for attempt in range(3):
        try:
            # 키는 헤더로 보낸다 — URL에 넣으면 오류 메시지·로그에 키가 찍힌다.
            response = await client.post(URL, headers={"x-goog-api-key": GEMINI_API_KEY}, json=body, timeout=timeout)
            if response.status_code in (429, 500, 502, 503, 504):
                last = f"HTTP {response.status_code}"
                await asyncio.sleep(2 * (attempt + 1))
                continue
            if response.status_code >= 400:  # 요청 형식 오류는 다시 보내도 같다 — 본문(오류 설명)만 남기고 멈춘다
                raise RuntimeError(f"HTTP {response.status_code}: {response.text[:600]}")
            data = response.json()
            stages = json.loads(data["candidates"][0]["content"]["parts"][0]["text"])["stages"]
            usage = data.get("usageMetadata", {})
            if len(stages) != n:
                last = f"stages {len(stages)} != {n}"
                continue
            return {"stages": stages, "prompt": usage.get("promptTokenCount", 0), "output": usage.get("candidatesTokenCount", 0),
                    "thinking": usage.get("thoughtsTokenCount", 0)}
        except (httpx.HTTPError, KeyError, json.JSONDecodeError) as exc:
            last = type(exc).__name__  # 예외 문자열에는 요청 정보가 들어갈 수 있어 이름만
            await asyncio.sleep(2 * (attempt + 1))
    raise RuntimeError(f"call failed: {last}")


def _image(data: bytes) -> dict:
    return {"inlineData": {"mimeType": "image/jpeg", "data": base64.b64encode(data).decode()}}


def _cost(prompt: int, output: int) -> float:
    return prompt / 1e6 * GEMINI_FLASH_INPUT_PRICE_PER_1M + output / 1e6 * GEMINI_FLASH_OUTPUT_PRICE_PER_1M


async def run(args):
    rows, vectors = load_project(args.project_id)
    n = len(rows)
    image_tokens = IMAGE_TOKENS[args.resolution]
    if args.mode == "batch":
        windows = batch_windows(n, args.batch, args.overlap)
        images = sum(end - start for start, end, _ in windows)
        est_prompt, calls = images * image_tokens + len(windows) * 1300, len(windows)
        est_output = len(windows) * (args.batch * 12 + 30)
    else:
        if vectors is None:
            sys.exit("임베딩이 없는 사진이 있어 구간을 만들 수 없습니다(유사컷 분석을 먼저).")
        segments = embedding_segments(vectors, args.window, args.threshold, args.min_len)
        images, calls = len(segments) * 2, 1
        est_prompt, est_output = images * image_tokens + 1500 + len(segments) * 12, len(segments) * 12 + 30
    print(f"사진 {n}장 · 모드 {args.mode} · 해상도 {args.resolution} · 호출 {calls}회 · 이미지 {images}장")
    print(f"예상 입력 {est_prompt:,} 토큰, 출력 {est_output:,} 토큰 → 약 ${_cost(est_prompt, est_output):.3f} (표준 단가, thinking 제외)")
    if args.estimate:
        return
    if not GEMINI_API_KEY:
        sys.exit("GEMINI_API_KEY가 없습니다(clip-service/.env).")
    started = time.time()
    lines = "\n".join(f"- {name}: {hint}" for name, hint in HINTS[args.hints].items())
    labels: list = [None] * n
    totals = Counter()
    async with httpx.AsyncClient() as client:
        if args.mode == "batch":
            sem = asyncio.Semaphore(args.concurrency)

            async def one(start, end, keep):
                async with sem:
                    data = await download_all([row["preview_url"] for row in rows[start:end]])
                    parts = [{"text": BATCH_PROMPT_T.format(n=end - start, stage_lines=lines)}]
                    for k, image in enumerate(data):
                        parts.append({"text": f"사진 {k + 1}"})
                        parts.append(_image(image) if image else {"text": "(이미지 없음)"})
                    result = await _call(client, parts, end - start, args.resolution, args.timeout)
                    for k in range(keep, end - start):
                        labels[start + k] = result["stages"][k]
                    totals.update(prompt=result["prompt"], output=result["output"], thinking=result["thinking"], calls=1)
                    print(f"  {start + 1:04}-{end:04} 완료", flush=True)

            await asyncio.gather(*[one(*window) for window in windows])
        else:
            picks = [(a + (b - a) // 3, a + 2 * (b - a) // 3) for a, b in segments]
            urls = [rows[i]["thumb_url"] or rows[i]["preview_url"] for pair in picks for i in pair]
            data = await download_all(urls)
            parts = [{"text": FLOW_PROMPT_T.format(k=len(segments), stage_lines=lines)}]
            for k, (a, b) in enumerate(segments):
                parts.append({"text": f"구간 {k} (사진 {b - a}장)"})
                for image in data[2 * k:2 * k + 2]:
                    if image:
                        parts.append(_image(image))
            result = await _call(client, parts, len(segments), args.resolution, args.timeout)
            for (a, b), stage in zip(segments, result["stages"]):
                for i in range(a, b):
                    labels[i] = stage
            totals.update(prompt=result["prompt"], output=result["output"], thinking=result["thinking"], calls=1)
    cost = _cost(totals["prompt"], totals["output"] + totals["thinking"])
    out = {"mode": args.mode, "resolution": args.resolution, "model": GEMINI_FLASH_MODEL, "prompt": f"stage-experiment-{args.hints}",
           "settings": {k: v for k, v in vars(args).items() if k not in ("func",)},
           "order": [row["id"] for row in rows], "labels": {row["id"]: label for row, label in zip(rows, labels)},
           "usage": dict(totals), "cost_usd": round(cost, 4), "seconds": round(time.time() - started, 1)}
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False))
    print(f"실제 입력 {totals['prompt']:,} · 출력 {totals['output']:,} · thinking {totals['thinking']:,} 토큰 → ${cost:.4f}, {out['seconds']}초 → {args.out}")


# --- 채점 ---------------------------------------------------------------------------------------------------------

def _runs(labels: list) -> list[list]:
    out = []
    for i, label in enumerate(labels):
        if out and out[-1][2] == label:
            out[-1][1] = i + 1
        else:
            out.append([i, i + 1, label])
    return out


def _matched(a: list[int], b: list[int], tol: int) -> int:
    used, hit = set(), 0
    for x in a:
        for j, y in enumerate(b):
            if j not in used and abs(x - y) <= tol:
                used.add(j)
                hit += 1
                break
    return hit


def metrics(pred: list, truth: list) -> dict:
    tb, pb = [s for s, _, _ in _runs(truth)[1:]], [s for s, _, _ in _runs(pred)[1:]]
    names = [label for *_, label in _runs(pred)]
    return {"acc": round(100 * sum(p == t for p, t in zip(pred, truth)) / len(truth), 1), "scenes": len(names),
            "P3": round(100 * _matched(pb, tb, 3) / max(1, len(pb))), "R3": round(100 * _matched(tb, pb, 3) / max(1, len(tb))),
            "P10": round(100 * _matched(pb, tb, 10) / max(1, len(pb))), "R10": round(100 * _matched(tb, pb, 10) / max(1, len(tb))),
            "repeat": sum(c - 1 for c in Counter(names).values() if c > 1)}


def smooth_current(raw: list, window: int = 21, min_run: int = 10) -> list:
    """지금 엔진(STAGE_WINDOW·STAGE_MIN_RUN) 다듬기 그대로."""
    known = [label if label and label != UNKNOWN else None for label in raw]
    if not any(known):
        return raw
    out = [None] * len(raw)
    for start, end, label in _label_runs(known, window, min_run):
        for i in range(start, end):
            out[i] = label
    return out


def smooth_segments(raw: list, vectors, min_run: int = 15) -> list:
    """임베딩 구간 안 다수결 + 짧은 조각은 원래 판정을 보고 양쪽 이웃으로 나눠 붙인다(오프라인 최고안)."""
    seg_labels = []
    for a, b in embedding_segments(vectors):
        counts = Counter(label for label in raw[a:b] if label and label != UNKNOWN)
        seg_labels += [counts.most_common(1)[0][0] if counts else UNKNOWN] * (b - a)
    runs = [list(run) for run in _runs(seg_labels)]
    while len(runs) > 1 and min(e - s for s, e, _ in runs) < min_run:
        k = min(range(len(runs)), key=lambda i: runs[i][1] - runs[i][0])
        s, e, _ = runs[k]
        if 0 < k < len(runs) - 1:
            left, right = runs[k - 1][2], runs[k + 1][2]
            cut = max(range(s, e + 1), key=lambda c: sum(raw[i] == left for i in range(s, c)) + sum(raw[i] == right for i in range(c, e)))
            runs[k - 1][1], runs[k + 1][0] = cut, cut
            del runs[k]
        else:
            j = 1 if k == 0 else k - 1
            lo = min(k, j)
            runs[lo:lo + 2] = [[runs[lo][0], runs[lo + 1][1], runs[j][2]]]
        merged = []
        for run in runs:
            if run[1] <= run[0]:
                continue
            if merged and merged[-1][2] == run[2]:
                merged[-1][1] = run[1]
            else:
                merged.append(run)
        runs = merged
    out = [None] * len(raw)
    for s, e, label in runs:
        for i in range(s, e):
            out[i] = label
    return out


def score(args):
    db = get_supabase()
    rows, vectors = load_project(args.project_id)
    if args.truth:
        segments = json.loads(Path(args.truth).read_text())["segments"]
        truth = [None] * len(rows)
        for name, start, end in segments:
            for i in range(start - 1, end):
                truth[i] = name
        label = [{"updated_at": f"{Path(args.truth).name} (로컬 파일)"}]
    else:
        label = db.table("customer_scene_labels").select("scenes,updated_at").eq("project_id", args.project_id).limit(1).execute().data
        if not label:
            sys.exit("관리자 검수 정답이 없습니다(/admin/scenes에서 저장하거나 --truth로 파일 지정).")
        truth_of = {pid: scene["name"] for scene in label[0]["scenes"] for pid in scene["photoIds"]}
        truth = [truth_of.get(row["id"]) for row in rows]
    sources = {}
    stored = {r["photo_id"]: r["place"] for r in _all_rows(lambda: db.table("customer_quality_assessments")
              .select("photo_id,place:raw_response->>place").eq("project_id", args.project_id).order("photo_id"))}
    if any(stored.values()):
        sources["저장된 사진별 판정(지금)"] = [stored.get(row["id"]) for row in rows]
    for path in args.results:
        data = json.loads(Path(path).read_text())
        sources[f"{Path(path).name} ({data['mode']}, {data['resolution']}, ${data['cost_usd']})"] = [data["labels"].get(row["id"]) for row in rows]
    print(f"정답 {len(_runs(truth))}개 장면 (저장 {label[0]['updated_at'][:16]})")
    print(f"{'판정 / 다듬기':<58}{'acc':>6}{'장면':>5}{'P3':>5}{'R3':>5}{'P10':>5}{'R10':>5}{'반복':>5}")
    for name, raw in sources.items():
        variants = {"사진별 그대로": raw, "지금 다듬기(창21·최소10)": smooth_current(raw)}
        if vectors is not None:
            variants["임베딩 구간 다듬기"] = smooth_segments(raw, vectors)
        for vname, pred in variants.items():
            m = metrics(pred, truth)
            print(f"{(name + ' · ' + vname)[:57]:<58}{m['acc']:>6}{m['scenes']:>5}{m['P3']:>5}{m['R3']:>5}{m['P10']:>5}{m['R10']:>5}{m['repeat']:>5}")
        confusion = Counter((t, p) for t, p in zip(truth, raw) if t != p)
        print("    사진별 오판정 상위:", ", ".join(f"{t}→{p} {c}" for (t, p), c in confusion.most_common(5)))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run")
    r.add_argument("project_id")
    r.add_argument("--mode", choices=["batch", "flow"], required=True)
    r.add_argument("--resolution", choices=list(RESOLUTION), default="default")
    r.add_argument("--hints", choices=list(HINTS), default="v2")
    r.add_argument("--batch", type=int, default=10)
    r.add_argument("--overlap", type=int, default=2)
    r.add_argument("--concurrency", type=int, default=4)
    r.add_argument("--window", type=int, default=5)
    r.add_argument("--threshold", type=float, default=0.90)
    r.add_argument("--min-len", type=int, default=4)
    r.add_argument("--timeout", type=float, default=120)
    r.add_argument("--estimate", action="store_true", help="호출 없이 예상 비용만")
    r.add_argument("--out", default="stage_experiment.json")
    s = sub.add_parser("score")
    s.add_argument("project_id")
    s.add_argument("results", nargs="*")
    s.add_argument("--truth", help="정답 파일(파일명 연번 구간). 없으면 관리자 검수 정답")
    args = parser.parse_args()
    if args.command == "run":
        asyncio.run(run(args))
    else:
        score(args)


if __name__ == "__main__":
    main()
