"""
backend.py — everything except the UI lives here:
  - config / env loading
  - Tavily + YouTube + retry/JSON utilities
  - Agent 1: web research
  - Agent 2: YouTube research
  - Consolidator (dedup)
  - Agent 3: diagram/image generation
  - Word document builder
  - LangGraph orchestration (run_pipeline is the single entry point app.py calls)

Flow:
                 START
                /      \\
      web_research   youtube_research     <- run in parallel per company/technology
                \\      /
                consolidate                <- merge + dedupe near-duplicate Q&A
                     |
                 visualize                 <- diagram (Graphviz) or fallback image per Q&A
                     |
               build_document              <- writes the final .docx
                     |
                    END
"""
import os
import re
import json
import html
import time
import uuid
import difflib
import functools
import logging
import threading
from urllib.parse import urlparse
from typing import TypedDict, List, Optional, Annotated
import operator

import requests
from dotenv import load_dotenv
from openai import OpenAI
from google import genai
from tavily import TavilyClient
from graphviz import Source
from docx import Document
from docx.shared import Inches, Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
import yt_dlp
from youtube_transcript_api import YouTubeTranscriptApi, TranscriptsDisabled, NoTranscriptFound
from langgraph.graph import StateGraph, START, END

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
load_dotenv()

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openai/gpt-oss-20b")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
TRANSCRIPT_API_URL = "https://youtube-transcript-api-tau-one.vercel.app/transcript"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(BASE_DIR, "outputs")
ASSETS_DIR = os.path.join(OUTPUT_DIR, "assets")
os.makedirs(ASSETS_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("backend")

_cancel_event = threading.Event()


class PipelineCancelled(Exception):
    """Raised when the user resets or refreshes during an active pipeline."""


def cancel_pipeline() -> None:
    _cancel_event.set()
    log.info("Pipeline cancellation requested.")


def _check_cancelled() -> None:
    if _cancel_event.is_set():
        raise PipelineCancelled("Analysis cancelled by reset or page refresh.")


def _start_pipeline() -> None:
    _cancel_event.clear()


def reset_generated_outputs() -> int:
    """Delete generated visuals and interview documents, leaving other files intact."""
    cancel_pipeline()
    deleted = 0
    generated_files = [
        (ASSETS_DIR, ("diagram_", "image_"), None),
        (OUTPUT_DIR, ("interview_prep_",), ".docx"),
    ]
    for directory, prefixes, suffix in generated_files:
        for name in os.listdir(directory):
            if not name.startswith(prefixes) or (suffix and not name.endswith(suffix)):
                continue
            path = os.path.join(directory, name)
            if os.path.isfile(path):
                try:
                    os.remove(path)
                    deleted += 1
                except OSError as exc:
                    log.warning("Could not delete generated output %s: %s", path, exc)
    log.info("Reset generated outputs: deleted %d file(s)", deleted)
    return deleted


def validate_config():
    missing = [
        name for name, val in [
            ("TAVILY_API_KEY", TAVILY_API_KEY),
        ] if not val
    ]
    if not OPENROUTER_API_KEY and not GEMINI_API_KEY:
        missing.append("OPENROUTER_API_KEY or GEMINI_API_KEY")
    if missing:
        raise EnvironmentError(
            f"Missing required environment variable(s): {', '.join(missing)}. "
            f"Create a .env file with these keys (see .env.example)."
        )


_openrouter = None
_gemini = None
_tavily = None


def _openrouter_client() -> OpenAI:
    global _openrouter
    if _openrouter is None:
        _openrouter = OpenAI(
            api_key=OPENROUTER_API_KEY,
            base_url="https://openrouter.ai/api/v1",
            default_headers={
                "HTTP-Referer": os.getenv("OPENROUTER_SITE_URL", "http://localhost"),
                "X-Title": os.getenv("OPENROUTER_APP_NAME", "Agentic Interview Prep Guide"),
            },
        )
    return _openrouter


def _gemini_client() -> genai.Client:
    global _gemini
    if _gemini is None:
        if not GEMINI_API_KEY:
            raise RuntimeError("GEMINI_API_KEY is not configured.")
        _gemini = genai.Client(api_key=GEMINI_API_KEY)
    return _gemini


def _generate_text(prompt: str, temperature: float, max_tokens: int) -> str:
    """Generate text with OpenRouter first and Gemini as the provider fallback."""
    openrouter_error = None
    if OPENROUTER_API_KEY:
        try:
            response = _openrouter_client().chat.completions.create(
                model=OPENROUTER_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=temperature,
                max_tokens=max_tokens,
            )
            text = (response.choices[0].message.content or "").strip()
            if text:
                return text
            raise RuntimeError("OpenRouter returned an empty response.")
        except Exception as exc:  # noqa: BLE001
            openrouter_error = exc
            log.warning("OpenRouter failed; trying Gemini fallback: %s", exc)

    if GEMINI_API_KEY:
        response = _gemini_client().models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config={
                "temperature": temperature,
                "max_output_tokens": max_tokens,
            },
        )
        text = (response.text or "").strip()
        if text:
            return text
        raise RuntimeError("Gemini returned an empty response.")

    if openrouter_error:
        raise openrouter_error
    raise RuntimeError("No LLM provider is configured.")


def _tavily_client() -> TavilyClient:
    global _tavily
    if _tavily is None:
        _tavily = TavilyClient(api_key=TAVILY_API_KEY)
    return _tavily


# --------------------------------------------------------------------------
# Small utilities
# --------------------------------------------------------------------------
def with_retry(max_attempts: int = 3, base_delay: float = 1.5):
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return fn(*args, **kwargs)
                except Exception as exc:  # noqa: BLE001
                    last_exc = exc
                    log.warning("%s attempt %d/%d failed: %s", fn.__name__, attempt, max_attempts, exc)
                    if attempt < max_attempts:
                        time.sleep(base_delay * attempt)
            raise last_exc
        return wrapper
    return decorator


def safe_json_list(raw: str) -> list:
    if not raw:
        return []
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
    start, end = raw.find("["), raw.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return []
    try:
        parsed = json.loads(raw[start:end + 1])
        return parsed if isinstance(parsed, list) else []
    except json.JSONDecodeError:
        return []


# --------------------------------------------------------------------------
# Tavily tools (web + image search)
# --------------------------------------------------------------------------
@with_retry()
def tavily_search_pages(company: str, technology: str, num_pages: int) -> list:
    company_term = f"{company.strip()} " if company.strip() else ""
    queries = [
        f"{company_term}{technology} interview questions and answers",
        f"{company_term}{technology} data engineer interview experience questions",
    ]
    candidate_limit = min(max(num_pages * 3, num_pages), 30)
    pages = []
    seen_urls = set()
    for query in queries:
        if len(pages) >= num_pages:
            break
        log.info("Tavily web search: %r (need %d non-YouTube pages)", query, num_pages)
        resp = _tavily_client().search(
            query=query, search_depth="advanced", max_results=candidate_limit,
            include_raw_content=True,
        )
        for result in resp.get("results", []):
            url = result.get("url", "")
            hostname = urlparse(url).hostname or ""
            if (hostname.lower().endswith("youtube.com")
                    or hostname.lower() in {"youtu.be", "www.youtu.be"}
                    or url in seen_urls):
                continue
            seen_urls.add(url)
            pages.append(result)
            if len(pages) == num_pages:
                break
    log.info("Tavily web search: retained %d/%d non-YouTube pages", len(pages), num_pages)
    return pages


@with_retry()
def tavily_search_images(query: str, max_results: int = 1) -> list:
    log.info("Tavily image search: %r", query)
    resp = _tavily_client().search(
        query=query, search_depth="basic", max_results=max_results, include_images=True,
    )
    return resp.get("images", [])


# --------------------------------------------------------------------------
# YouTube tools (free, keyless: yt-dlp search + youtube-transcript-api)
# --------------------------------------------------------------------------
@with_retry()
def youtube_search(query: str, max_results: int = 5) -> list:
    ydl_opts = {"quiet": True, "skip_download": True, "extract_flat": True, "no_warnings": True}
    search_query = f"ytsearch{max_results}:{query}"
    log.info("YouTube search: %r (max %d)", query, max_results)
    videos = []
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        result = ydl.extract_info(search_query, download=False)
        for entry in (result or {}).get("entries", []) or []:
            if entry and entry.get("id"):
                videos.append({
                    "id": entry["id"], "title": entry.get("title", ""),
                    "url": f"https://www.youtube.com/watch?v={entry['id']}",
                })
    return videos


def youtube_transcript(video_id: str) -> str:
    try:
        # youtube-transcript-api 1.x exposes fetch() on an instance and returns
        # FetchedTranscriptSnippet objects rather than dictionaries.
        transcript = YouTubeTranscriptApi().fetch(video_id, languages=("en",))
        return " ".join(snippet.text for snippet in transcript)
    except (TranscriptsDisabled, NoTranscriptFound):
        pass
    except Exception as exc:  # noqa: BLE001
        log.warning("Transcript API failed for %s; trying yt-dlp captions: %s", video_id, exc)

    try:
        video_url = f"https://www.youtube.com/watch?v={video_id}"
        response = requests.post(
            TRANSCRIPT_API_URL,
            json={"url": video_url},
            timeout=30,
        )
        response.raise_for_status()
        transcript = response.json().get("transcript")
        if isinstance(transcript, str) and transcript.strip():
            try:
                decoded = json.loads(transcript)
            except json.JSONDecodeError:
                decoded = None
            malformed = (
                isinstance(decoded, (dict, list))
                or transcript.lstrip().startswith(("0:{", "1:{"))
                or '"$@' in transcript
            )
            if malformed:
                log.warning("Hosted transcript response was not plain text for %s", video_id)
            else:
                log.info("Hosted transcript fallback succeeded for %s", video_id)
                return transcript
    except requests.RequestException as exc:
        log.warning("Hosted transcript fallback failed for %s: %s", video_id, exc)
    except (TypeError, ValueError) as exc:
        log.warning("Invalid hosted transcript response for %s: %s", video_id, exc)

    try:
        ydl_opts = {"quiet": True, "skip_download": True, "no_warnings": True}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(
                f"https://www.youtube.com/watch?v={video_id}", download=False,
            )
            captions = info.get("subtitles") or info.get("automatic_captions") or {}
            tracks = captions.get("en") or captions.get("en-US") or []
            track = next((item for item in tracks if item.get("ext") == "vtt"), None)
            if not track and tracks:
                track = tracks[0]
            if not track:
                return ""
            caption_data = ydl.urlopen(track["url"]).read().decode("utf-8", errors="replace")
            lines = []
            for line in caption_data.splitlines():
                line = line.strip()
                if not line or line == "WEBVTT" or "-->" in line or line.isdigit():
                    continue
                line = re.sub(r"<[^>]+>", "", line)
                line = html.unescape(line).strip()
                if line and (not lines or line != lines[-1]):
                    lines.append(line)
            return " ".join(lines)
    except Exception as exc:  # noqa: BLE001
        log.warning("yt-dlp caption fetch failed for %s: %s", video_id, exc)

    try:
        video_url = f"https://www.youtube.com/watch?v={video_id}"
        extracted = _tavily_client().extract([video_url])
        results = extracted.get("results", [])
        if results:
            content = results[0].get("raw_content") or results[0].get("content") or ""
            if content.strip():
                log.info("Tavily transcript fallback succeeded for %s", video_id)
                return content
    except Exception as exc:  # noqa: BLE001
        log.warning("Tavily transcript fallback failed for %s: %s", video_id, exc)
    return ""


# --------------------------------------------------------------------------
# State schema
# --------------------------------------------------------------------------
class QAItem(TypedDict):
    question: str
    answer: str
    source_type: str
    source_url: str
    technology: str
    company: str
    visual_path: Optional[str]
    visual_type: Optional[str]
    visual_caption: Optional[str]
    frequency_count: int
    importance_score: int


class CompanyState(TypedDict):
    company: str
    technologies: List[str]
    num_pages: int
    web_qas: List[QAItem]
    youtube_qas: List[QAItem]
    consolidated_qas: List[QAItem]


class GraphState(TypedDict):
    companies: List[str]
    technologies: List[str]
    num_pages: int
    max_questions: int
    company_results: Annotated[List[CompanyState], operator.add]  # raw accumulator (parallel branches)
    final_results: List[CompanyState]                              # merged/consolidated (plain overwrite)
    output_path: Optional[str]
    log: Annotated[List[str], operator.add]


def _empty_company_state(company: str, technologies: list, num_pages: int) -> dict:
    return {
        "company": company, "technologies": technologies, "num_pages": num_pages,
        "web_qas": [], "youtube_qas": [], "consolidated_qas": [],
    }


def _company_context(company: str) -> str:
    return company.strip() if company.strip() else "general technical interviews"


# --------------------------------------------------------------------------
# Agent 1: Web research
# --------------------------------------------------------------------------
WEB_EXTRACTION_PROMPT = """You are an expert technical interviewer. From the web page text below, \
extract genuine interview question-and-answer pairs relevant to "{technology}" for "{company_context}".

Rules:
- Only include items that are clearly technical interview-style questions (not ads/navigation/generic articles).
- If a question appears without a full answer in the text, write a concise, correct answer yourself.
- If nothing relevant is found, return an empty list.

Return STRICT JSON ONLY — a list like [{{"question": "...", "answer": "..."}}]. No prose, no markdown fences.

PAGE TEXT:
{content}
"""


@with_retry()
def _llm_extract(prompt: str) -> list:
    return safe_json_list(_generate_text(prompt, temperature=0.2, max_tokens=2048))


def run_web_research(company: str, technology: str, num_pages: int) -> list:
    pages = tavily_search_pages(company, technology, num_pages)
    qas = []
    for page in pages:
        _check_cancelled()
        content = (page.get("raw_content") or page.get("content") or "").strip()[:8000]
        if not content.strip():
            log.warning("Skipping web result without content: %s", page.get("url", ""))
            continue
        url = page.get("url", "")
        try:
            parsed = _llm_extract(WEB_EXTRACTION_PROMPT.format(
                technology=technology, company_context=_company_context(company), content=content,
            ))
        except Exception as exc:  # noqa: BLE001
            log.warning("Extraction failed for %s: %s", url, exc)
            parsed = []
        for item in parsed:
            q, a = item.get("question", "").strip(), item.get("answer", "").strip()
            if q and a:
                qas.append({
                    "question": q, "answer": a, "source_type": "web", "source_url": url,
                    "technology": technology, "company": company,
                    "visual_path": None, "visual_type": None, "visual_caption": None,
                    "frequency_count": 1, "importance_score": 1,
                })
    log.info("Web research: %s / %s -> %d Q&A from %d pages", company, technology, len(qas), len(pages))
    return qas


# --------------------------------------------------------------------------
# Agent 2: YouTube research
# --------------------------------------------------------------------------
YT_EXTRACTION_PROMPT = """You are an expert technical interviewer. From the video transcript below, \
extract genuine interview question-and-answer pairs relevant to "{technology}" for "{company_context}".

Rules:
- Only include items clearly discussed as interview-style questions in the transcript.
- If the transcript doesn't give a complete answer, write a concise, correct answer yourself.
- If nothing relevant is found, return an empty list.

Return STRICT JSON ONLY — a list like [{{"question": "...", "answer": "..."}}]. No prose, no markdown fences.

TRANSCRIPT:
{content}
"""


def run_youtube_research(company: str, technology: str, num_videos: int) -> list:
    company_term = f"{company.strip()} " if company.strip() else ""
    query = f"{company_term}{technology} interview questions"
    videos = youtube_search(query, max_results=num_videos)
    qas = []
    for video in videos:
        _check_cancelled()
        transcript = youtube_transcript(video["id"])
        if not transcript.strip():
            continue
        window_size = 8000
        starts = sorted({0, max(0, len(transcript) // 2 - window_size // 2), max(0, len(transcript) - window_size)})
        for start in starts:
            _check_cancelled()
            content = transcript[start:start + window_size]
            try:
                parsed = _llm_extract(YT_EXTRACTION_PROMPT.format(
                    technology=technology, company_context=_company_context(company), content=content,
                ))
            except Exception as exc:  # noqa: BLE001
                log.warning("Extraction failed for %s: %s", video["url"], exc)
                parsed = []
            for item in parsed:
                q, a = item.get("question", "").strip(), item.get("answer", "").strip()
                if q and a:
                    qas.append({
                        "question": q, "answer": a, "source_type": "youtube", "source_url": video["url"],
                        "technology": technology, "company": company,
                        "visual_path": None, "visual_type": None, "visual_caption": None,
                        "frequency_count": 1, "importance_score": 1,
                    })
    log.info("YouTube research: %s / %s -> %d Q&A from %d videos", company, technology, len(qas), len(videos))
    return qas


# --------------------------------------------------------------------------
# Consolidator: dedupe near-identical questions (local, no API cost)
# --------------------------------------------------------------------------
def consolidate_qas(web_qas: list, youtube_qas: list, threshold: float = 0.80) -> list:
    combined = web_qas + youtube_qas
    unique = []
    for qa in combined:
        duplicate = next(
            (existing for existing in unique if difflib.SequenceMatcher(
                None, qa["question"].lower(), existing["question"].lower()
            ).ratio() > threshold),
            None,
        )
        if duplicate is not None:
            duplicate["frequency_count"] += 1
        else:
            consolidated = dict(qa)
            consolidated["frequency_count"] = 1
            consolidated["importance_score"] = 1
            unique.append(consolidated)

    max_frequency = max((qa["frequency_count"] for qa in unique), default=1)
    for qa in unique:
        qa["importance_score"] = max(1, round(10 * qa["frequency_count"] / max_frequency))
    log.info("Consolidated %d raw Q&A -> %d unique", len(combined), len(unique))
    return unique


# --------------------------------------------------------------------------
# Agent 3: Diagram / image per question
# --------------------------------------------------------------------------
DIAGRAM_PROMPT = """You explain technical interview answers visually.
For the specific Q&A below, produce a SIMPLE Graphviz DOT diagram (a "digraph") with at most 6 nodes \
that visually explains the unique concept, process, or architecture in this answer. The diagram \
must use labels and relationships specific to this question; do not produce a generic Databricks, \
cloud, or interview diagram. Use short node labels. Return ONLY the raw DOT code, starting with \
'digraph' — no prose, no markdown fences.

QUESTION: {question}
ANSWER: {answer}
"""


@with_retry(max_attempts=2)
def _generate_dot(question: str, answer: str) -> str:
    raw = _generate_text(
        DIAGRAM_PROMPT.format(question=question, answer=answer[:1500]),
        temperature=0.3,
        max_tokens=600,
    )
    match = re.search(r"digraph[\s\S]*}", raw)
    return match.group(0) if match else ""


PLAIN_LANGUAGE_PROMPT = """Rewrite the technical answer below so it is easy for a non-technical reader to understand.

Rules:
- Keep the answer accurate and preserve important technology names.
- Explain every unavoidable technical term in simple words the first time it appears.
- Prefer short sentences, everyday words, and a practical analogy or example when useful.
- Explain what the concept does, why it matters, and how it works at a high level.
- Do not mention this rewriting instruction, the source text, or that you are an AI.
- Return only the rewritten explanation, with no heading and no markdown.

QUESTION:
{question}

TECHNICAL ANSWER:
{answer}
"""


@with_retry(max_attempts=2)
def _simplify_answer(question: str, answer: str) -> str:
    simplified = _generate_text(
        PLAIN_LANGUAGE_PROMPT.format(question=question, answer=answer[:3000]),
        temperature=0.2,
        max_tokens=1000,
    )
    return simplified or answer


def _render_dot(dot_code: str):
    try:
        file_id = uuid.uuid4().hex[:10]
        out_path = os.path.join(ASSETS_DIR, f"diagram_{file_id}")
        return Source(dot_code, format="png").render(out_path, cleanup=True)
    except Exception as exc:  # noqa: BLE001 — Graphviz binary missing / bad DOT, etc.
        log.warning("Diagram render failed: %s", exc)
        return None


def _fetch_fallback_image(qa: dict):
    try:
        query = f"{qa['technology']} {qa['company']} {qa['question']} technical diagram"
        images = tavily_search_images(query, max_results=3)
        if not images:
            return None
        resp = requests.get(images[0], timeout=10)
        if resp.status_code == 200 and resp.content:
            local_path = os.path.join(ASSETS_DIR, f"image_{uuid.uuid4().hex[:10]}.jpg")
            with open(local_path, "wb") as f:
                f.write(resp.content)
            return local_path
    except Exception as exc:  # noqa: BLE001
        log.warning("Fallback image fetch failed: %s", exc)
    return None


def add_visual(qa: dict) -> dict:
    try:
        dot_code = _generate_dot(qa["question"], qa["answer"])
    except Exception as exc:  # noqa: BLE001
        log.warning("DOT generation failed: %s", exc)
        dot_code = ""

    if dot_code:
        image_path = _render_dot(dot_code)
        if image_path:
            qa["visual_path"], qa["visual_type"] = image_path, "diagram"
            qa["visual_caption"] = f"Concept diagram — {qa['question'][:90]}"
            return qa

    local_path = _fetch_fallback_image(qa)
    if local_path:
        qa["visual_path"], qa["visual_type"] = local_path, "image"
        qa["visual_caption"] = f"Reference image — {qa['question'][:90]}"
    return qa


# --------------------------------------------------------------------------
# Word document builder
# --------------------------------------------------------------------------
def build_document(company_results: list, output_path: str) -> str:
    doc = Document()
    title = doc.add_heading("Interview Preparation Guide", level=0)
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER

    subtitle = doc.add_paragraph()
    subtitle.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = subtitle.add_run("Auto-generated research with plain-language explanations")
    run.italic, run.font.size, run.font.color.rgb = True, Pt(10), RGBColor(0x55, 0x55, 0x55)
    doc.add_page_break()

    doc.add_heading("Contents", level=1)
    for cr in company_results:
        doc.add_paragraph(cr["company"] or "General technical interviews", style="List Bullet")
    doc.add_page_break()

    for cr in company_results:
        doc.add_heading(cr["company"] or "General technical interviews", level=1)
        by_tech = {}
        for qa in cr["consolidated_qas"]:
            by_tech.setdefault(qa["technology"], []).append(qa)

        if not by_tech:
            doc.add_paragraph("No interview questions could be confidently extracted for this company/technology.")

        for tech, qas in by_tech.items():
            doc.add_heading(tech, level=2)
            for idx, qa in enumerate(qas, start=1):
                doc.add_heading(f"Q{idx}. {qa['question']}", level=3)
                doc.add_paragraph(
                    f"Importance: {qa['importance_score']}/10 | "
                    f"Observed frequency: {qa['frequency_count']} source(s)"
                )
                doc.add_paragraph("Plain-language explanation:")
                doc.add_paragraph(qa["answer"])

                visual_path = qa.get("visual_path")
                if visual_path and os.path.exists(visual_path):
                    try:
                        doc.add_picture(visual_path, width=Inches(5.5))
                        doc.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER
                        caption_text = qa.get("visual_caption") or ""
                        if caption_text:
                            cap = doc.add_paragraph()
                            cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
                            cap_run = cap.add_run(caption_text)
                            cap_run.italic, cap_run.font.size = True, Pt(9)
                            cap_run.font.color.rgb = RGBColor(0x66, 0x66, 0x66)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("Could not embed image %s: %s", visual_path, exc)

                source_note = doc.add_paragraph()
                source_run = source_note.add_run(f"Source ({qa['source_type']}): {qa['source_url']}")
                source_run.font.size, source_run.italic = Pt(8), True
                source_run.font.color.rgb = RGBColor(0x88, 0x88, 0x88)
                doc.add_paragraph()

    doc.save(output_path)
    log.info("Document saved to %s", output_path)
    return output_path


# --------------------------------------------------------------------------
# LangGraph orchestration
# --------------------------------------------------------------------------
def _web_research_node(state: GraphState) -> dict:
    company_results, logs = [], []
    for company in state["companies"]:
        cr = _empty_company_state(company, state["technologies"], state["num_pages"])
        for tech in state["technologies"]:
            found = run_web_research(company, tech, state["num_pages"])
            cr["web_qas"].extend(found)
            logs.append(f"[web] {company} / {tech}: {len(found)} Q&A found")
        company_results.append(cr)
    return {"company_results": company_results, "log": logs}


def _youtube_research_node(state: GraphState) -> dict:
    company_results, logs = [], []
    for company in state["companies"]:
        cr = _empty_company_state(company, state["technologies"], state["num_pages"])
        for tech in state["technologies"]:
            found = run_youtube_research(company, tech, state["num_pages"])
            cr["youtube_qas"].extend(found)
            logs.append(f"[youtube] {company} / {tech}: {len(found)} Q&A found")
        company_results.append(cr)
    return {"company_results": company_results, "log": logs}


def _merge_by_company(company_results: list) -> list:
    merged = {}
    for cr in company_results:
        company = cr["company"]
        if company not in merged:
            merged[company] = _empty_company_state(company, cr["technologies"], cr["num_pages"])
        merged[company]["web_qas"].extend(cr["web_qas"])
        merged[company]["youtube_qas"].extend(cr["youtube_qas"])
    return list(merged.values())


def _consolidate_node(state: GraphState) -> dict:
    merged = _merge_by_company(state["company_results"])
    web_count = sum(len(cr["web_qas"]) for cr in merged)
    youtube_count = sum(len(cr["youtube_qas"]) for cr in merged)
    for cr in merged:
        cr["consolidated_qas"] = consolidate_qas(cr["web_qas"], cr["youtube_qas"])
    ranked = sorted(
        ((cr, qa) for cr in merged for qa in cr["consolidated_qas"]),
        key=lambda item: (item[1]["importance_score"], item[1]["frequency_count"]),
        reverse=True,
    )[:state["max_questions"]]
    selected_ids = {id(qa) for _, qa in ranked}
    for cr in merged:
        cr["consolidated_qas"] = sorted(
            (qa for qa in cr["consolidated_qas"] if id(qa) in selected_ids),
            key=lambda qa: (qa["importance_score"], qa["frequency_count"]),
            reverse=True,
        )
    selected_count = sum(len(cr["consolidated_qas"]) for cr in merged)
    return {
        "final_results": merged,
        "log": [
            f"Questions identified: {web_count} web, {youtube_count} YouTube.",
            f"Priority selection: {selected_count} question(s) retained.",
            "Consolidation complete.",
        ],
    }


def _visualize_node(state: GraphState) -> dict:
    updated = []
    total_images = sum(len(cr["consolidated_qas"]) for cr in state["final_results"])
    generated_images = 0
    logs = []
    for cr in state["final_results"]:
        new_qas = []
        for qa in cr["consolidated_qas"]:
            _check_cancelled()
            simplified_qa = dict(qa)
            try:
                simplified_qa["answer"] = _simplify_answer(qa["question"], qa["answer"])
            except Exception as exc:  # noqa: BLE001
                log.warning("Plain-language rewrite failed for %s: %s", qa["question"], exc)
            visualized_qa = add_visual(simplified_qa)
            if visualized_qa.get("visual_path"):
                generated_images += 1
            logs.append(
                f"Images generated: {generated_images}/{total_images} "
                f"({total_images - generated_images} remaining)."
            )
            new_qas.append(visualized_qa)
        updated.append({**cr, "consolidated_qas": new_qas})
    logs.append(
        f"Image generation complete: {generated_images} generated, "
        f"{total_images - generated_images} remaining."
    )
    return {"final_results": updated, "log": logs}


def _build_document_node(state: GraphState) -> dict:
    _check_cancelled()
    question_count = sum(len(cr["consolidated_qas"]) for cr in state["final_results"])
    if question_count == 0:
        message = (
            "No resources could be identified. Try another company, technology, "
            "or a generalized search."
        )
        log.warning(message)
        return {"output_path": None, "log": [message]}
    filename = f"interview_prep_{uuid.uuid4().hex[:8]}.docx"
    output_path = os.path.join(OUTPUT_DIR, filename)
    build_document(state["final_results"], output_path)
    return {"output_path": output_path, "log": [f"Document saved: {output_path}"]}


def build_graph():
    graph = StateGraph(GraphState)
    graph.add_node("web_research", _web_research_node)
    graph.add_node("youtube_research", _youtube_research_node)
    graph.add_node("consolidate", _consolidate_node)
    graph.add_node("visualize", _visualize_node)
    graph.add_node("build_document", _build_document_node)

    graph.add_edge(START, "web_research")
    graph.add_edge(START, "youtube_research")
    graph.add_edge("web_research", "consolidate")
    graph.add_edge("youtube_research", "consolidate")
    graph.add_edge("consolidate", "visualize")
    graph.add_edge("visualize", "build_document")
    graph.add_edge("build_document", END)
    return graph.compile()


def run_pipeline(companies: list, technologies: list, num_pages: int, on_update=None, max_questions: int = 20) -> dict:
    """
    Single entry point for the frontend.
    `on_update(node_name, node_partial_state)` is called after each graph node
    finishes, if provided — used to drive a progress bar / live log in the UI.
    Returns the final state dict, which includes 'output_path' and 'final_results'.
    """
    validate_config()
    _start_pipeline()
    graph = build_graph()
    initial_state = {
        "companies": companies, "technologies": technologies, "num_pages": num_pages,
        "max_questions": max_questions,
        "company_results": [], "final_results": [], "output_path": None, "log": [],
    }
    last_state = {}
    for step_output in graph.stream(initial_state, stream_mode="updates"):
        node_name = next(iter(step_output.keys()))
        node_partial = step_output[node_name]
        last_state.update(node_partial)
        if on_update:
            on_update(node_name, node_partial)
    return last_state
