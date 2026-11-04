"""PDF rendering of deliberation reports (spec §11) with ReportLab."""

import io
import math
from collections.abc import Callable
from functools import lru_cache
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.platypus import (
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app.services.reports.data import CriterionResult, FinalistReport, ReportData

GENERATOR_VERSION = "report-v2"  # v2: Times New Roman, 12 pt, 1.5 line spacing

INK = colors.HexColor("#17212b")
MUTED = colors.HexColor("#5b6876")
ACCENT = colors.HexColor("#1d3f5e")
RULE = colors.HexColor("#dde2e8")
SOFT = colors.HexColor("#f2f5f8")
WARN = colors.HexColor("#8a5a00")
WARN_SOFT = colors.HexColor("#fdf3e0")

_base = getSampleStyleSheet()
# Times New Roman (PDF's built-in Times), 12 pt text with 1.5 line spacing throughout;
# headings are larger and bold.
FONT, FONT_BOLD = "Times-Roman", "Times-Bold"
TEXT_SIZE = 12
LINE_SPACING = 1.5


def _style(name: str, size: float = TEXT_SIZE, bold: bool = False, **extra) -> ParagraphStyle:
    return ParagraphStyle(
        name,
        parent=_base["Normal"],
        fontName=FONT_BOLD if bold else FONT,
        fontSize=size,
        leading=size * LINE_SPACING,
        **extra,
    )


STYLES = {
    "title": _style("title", 18, bold=True, textColor=ACCENT, spaceAfter=2),
    "subtitle": _style("subtitle", textColor=MUTED, spaceAfter=10),
    "h2": _style("h2", 14, bold=True, textColor=ACCENT, spaceBefore=12, spaceAfter=6),
    "h3": _style("h3", 13, bold=True, textColor=INK, spaceBefore=8, spaceAfter=3),
    "body": _style("body", textColor=INK),
    "small": _style("small", textColor=MUTED),
    "quote": _style("quote", textColor=INK, leftIndent=8),
    "cell": _style("cell"),
    "cellc": _style("cellc", alignment=TA_CENTER),
    "notice": _style("notice", textColor=INK),
}


def p(text: str | None, style: str = "body") -> Paragraph:
    return Paragraph(escape(text or ""), STYLES[style])


def markup(html: str, style: str = "body") -> Paragraph:
    """For text built here with <b>/<font> tags around already-escaped values."""
    return Paragraph(html, STYLES[style])


def bullets(items: list[str], empty: str) -> list:
    if not items:
        return [p(empty, "small")]
    return [Paragraph("• " + escape(i), STYLES["body"]) for i in items]


def boxed(flowables: list, background: colors.Color = SOFT, border: colors.Color = RULE) -> Table:
    table = Table([[flowables]], colWidths=["100%"])
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), background),
                ("BOX", (0, 0), (-1, -1), 0.6, border),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    return table


def grid(rows: list[list], widths: list, header: bool = True) -> Table:
    table = Table(rows, colWidths=widths, repeatRows=1 if header else 0)
    style = [
        ("GRID", (0, 0), (-1, -1), 0.4, RULE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]
    if header:
        style.append(("BACKGROUND", (0, 0), (-1, 0), SOFT))
    table.setStyle(TableStyle(style))
    return table


@lru_cache
def _watermark(width: int, height: int) -> ImageReader:
    """The PRACTICE watermark as an image, not text, so it stays out of the PDF's text layer
    (search, copy and screen readers); the notice box states the same thing in text."""
    from PIL import Image, ImageDraw, ImageFont

    scale = 2  # pixels per point
    label = "PRACTICE: RUBRIC NOT FROZEN"
    diagonal = math.hypot(width, height) * scale
    font = ImageFont.load_default(size=int(diagonal / 22))
    left, top, right, bottom = font.getbbox(label)
    band = Image.new("RGBA", (right - left + 20, bottom - top + 20), (0, 0, 0, 0))
    ImageDraw.Draw(band).text((10 - left, 10 - top), label, font=font, fill=(217, 140, 26, 34))
    band = band.rotate(math.degrees(math.atan2(height, width)), expand=True)
    page = Image.new("RGBA", (width * scale, height * scale), (0, 0, 0, 0))
    page.alpha_composite(band, ((page.width - band.width) // 2, (page.height - band.height) // 2))
    buffer = io.BytesIO()
    page.save(buffer, "PNG")
    buffer.seek(0)
    return ImageReader(buffer)


def _page_decorator(data: ReportData, label: str) -> Callable:
    def decorate(canvas, doc) -> None:  # noqa: ANN001  (ReportLab callback signature)
        canvas.saveState()
        width, height = doc.pagesize
        if not data.production:
            canvas.drawImage(
                _watermark(round(width), round(height)), 0, 0, width, height, mask="auto"
            )
        # Page furniture stays small so the disclaimer fits on one line.
        canvas.setFont(FONT, 8)
        canvas.setFillColor(MUTED)
        canvas.drawString(
            doc.leftMargin,
            10 * mm,
            f"WOB Awards 2026 · {label} · Confidential: judging committee use only",
        )
        canvas.drawRightString(width - doc.rightMargin, 10 * mm, f"Page {doc.page}")
        canvas.drawString(doc.leftMargin, 6.5 * mm, data.disclaimer)
        canvas.restoreState()

    return decorate


def _build(story: list, data: ReportData, label: str, pagesize: tuple = A4) -> bytes:
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=pagesize,
        leftMargin=16 * mm,
        rightMargin=16 * mm,
        topMargin=15 * mm,
        bottomMargin=18 * mm,
        title=f"WOB Awards 2026 – {label}",
        author="WOB Awards evidence system",
        subject="Provisional deliberation report",
        creator=GENERATOR_VERSION,
    )
    decorate = _page_decorator(data, label)
    doc.build(story, onFirstPage=decorate, onLaterPages=decorate)
    return buffer.getvalue()


def _status_notice(data: ReportData) -> list:
    status = (
        "Provisional ranking on the committee's frozen rubric."
        if data.production
        else f"PRACTICE ONLY: rubric version {data.rubric_version} is {data.rubric_status.value}, "
        "not frozen. Do not use for decisions."
    )
    background, border = (SOFT, RULE) if data.production else (WARN_SOFT, WARN)
    return [
        boxed(
            [markup(f"<b>{escape(status)}</b>", "notice"), p(data.disclaimer, "notice")],
            background,
            border,
        ),
        Spacer(1, 8),
    ]


def _provenance(data: ReportData, finalist: FinalistReport | None) -> list:
    run = data.run
    retrieval = run.get("retrieval") or {}
    rows = [
        ["Ranking snapshot", f"{data.snapshot_id} ({data.snapshot_at:%d %b %Y %H:%M} UTC)"],
        ["Calculation", f"{data.calculation_version}, {data.weighting} weighting"],
        ["Rubric", f"version {data.rubric_version} ({data.rubric_status.value})"],
        ["Assessment run", f"{run['id']}"],
        ["Assessment model / prompt", f"{run['models']} / {run['prompt_version']}"],
        [
            "Retrieval",
            f"{run['embedding_model']}; reranker {run['reranker_model'] or 'none'}; "
            f"top {retrieval.get('top_k', '?')} passages",
        ],
        ["AI calibration (FR-15)", str(run.get("calibration", "not recorded"))],
        ["Report generated", f"{data.generated_at:%d %b %Y %H:%M} UTC by {GENERATOR_VERSION}"],
    ]
    story = [
        p("Provenance", "h2"),
        grid([[p(a, "cell"), p(b, "cell")] for a, b in rows], [45 * mm, None], header=False),
    ]
    if finalist and finalist.documents:
        story += [
            p("Evidence documents", "h3"),
            grid(
                [
                    [
                        p("Document", "cell"),
                        p("SHA-256", "cell"),
                        p("Uploaded", "cell"),
                        p("Status", "cell"),
                    ]
                ]
                + [
                    [
                        p(d.filename, "cell"),
                        p(d.sha256[:16] + "…", "cell"),
                        p(f"{d.uploaded_at:%d %b %Y}", "cell"),
                        p("withdrawn" if d.withdrawn else d.status, "cell"),
                    ]
                    for d in finalist.documents
                ],
                [70 * mm, 40 * mm, 28 * mm, None],
            ),
        ]
    story.append(
        p(
            "Scores and ranks come from the snapshot above. Rationales, reviews and claim "
            "statuses are shown as they stood when the report was generated.",
            "small",
        )
    )
    return story


def _criterion_block(r: CriterionResult) -> list:
    rating = r.snapshot_rating if r.snapshot_rating is not None else "not rated"
    head = f"<b>{escape(r.name)}</b> · rating in ranking: <b>{rating}</b>"
    if r.reviewer_rating is not None and r.ai_rating is not None:
        head += f" (AI proposed {r.ai_rating}, reviewer adjusted)"
    parts = [markup(head, "h3")]
    meta = [f"review: {r.status}"]
    if r.reviewed_by:
        meta.append(f"by {r.reviewed_by}")
    if r.sufficiency:
        meta.append(f"evidence {r.sufficiency}")
    parts.append(p(" · ".join(meta), "small"))
    if r.changed_since_snapshot and r.current_rating is not None:
        parts.append(
            p(
                f"Reviewed after this ranking was calculated: current rating {r.current_rating}.",
                "small",
            )
        )
    if r.rationale:
        parts.append(p(r.rationale))
    for c in r.citations:
        prefix = "Contradicts: " if c.relation == "contradicts" else ""
        parts.append(
            markup(
                f"{escape(prefix)}“{escape(c.quote)}” "
                f"<font color='#5b6876'>— {escape(c.source)}</font>",
                "quote",
            )
        )
    if r.limitations:
        parts.append(p(f"Limitations: {r.limitations}", "small"))
    if r.review_notes:
        parts.append(p(f"Reviewer notes: {r.review_notes}", "small"))
    if r.flags:
        parts.append(p("Flags: " + ", ".join(r.flags), "small"))
    return [KeepTogether(parts[:3]), *parts[3:], Spacer(1, 4)]


def render_individual(data: ReportData, f: FinalistReport) -> bytes:
    rank = f"{f.rank} of {len(data.finalists)}" if f.rank else "not ranked (incomplete)"
    if f.is_tie:
        rank += f", tied with {', '.join(f.tied_with)}"
    claims = ", ".join(f"{n} {s}" for s, n in sorted(f.claim_counts.items())) or "none extracted"
    story = [
        p(f.name, "title"),
        p(" · ".join(x for x in (f.position, f.organisation) if x) or " ", "subtitle"),
        p(f"{data.category} · Individual deliberation report", "subtitle"),
        *_status_notice(data),
        grid(
            [
                [
                    p("Provisional rank", "cell"),
                    p(f"Score ({data.scale.label})", "cell"),
                    p("Out of 100", "cell"),
                    p("Criteria rated", "cell"),
                    p("Awaiting review", "cell"),
                ],
                [
                    p(rank, "cell"),
                    p(f.score_display or "—", "cell"),
                    p(f.points_display or "—", "cell"),
                    p(f"{f.criteria_assessed} of {f.criteria_total}", "cell"),
                    p(str(len(f.unreviewed)), "cell"),
                ],
            ],
            [None, 28 * mm, 26 * mm, 28 * mm, 30 * mm],
        ),
        p("Criterion ratings", "h2"),
        grid(
            [
                [
                    p("Criterion", "cell"),
                    p("Rating", "cellc"),
                    p("Review", "cell"),
                    p("Evidence", "cell"),
                ]
            ]
            + [
                [
                    p(r.name, "cell"),
                    p(str(r.snapshot_rating) if r.snapshot_rating is not None else "—", "cellc"),
                    p(r.status, "cell"),
                    p(r.sufficiency or "—", "cell"),
                ]
                for r in f.criteria
            ],
            [None, 18 * mm, 28 * mm, 28 * mm],
        ),
        p("Key strengths", "h2"),
        *bullets(f.strengths, "No criterion was rated 4 or above."),
        p("Limitations", "h2"),
        *bullets(f.limitations, "No criterion was rated 2 or below."),
        p("Verification", "h2"),
        p(
            f"Claims (as of report generation): {claims}. "
            f"Accepted public sources: {f.accepted_public_sources}."
        ),
        p("Unresolved claims", "h3"),
        *bullets([f"[{status}] {statement}" for statement, status in f.unresolved_claims], "None."),
        p("Unresolved contradictions", "h3"),
        *bullets(f.unresolved_contradictions, "None."),
        p("Questions for judges", "h2"),
        *bullets(f.questions, "No open questions arise from the record."),
        PageBreak(),
        p("Assessment detail", "h2"),
    ]
    for r in f.criteria:
        story += _criterion_block(r)
    story += _provenance(data, f)
    return _build(story, data, f"Individual report: {f.name}")


def render_category(data: ReportData) -> bytes:
    criteria = data.criteria
    header = [p("Rank", "cell"), p("Finalist", "cell"), p("Score", "cellc"), p("/100", "cellc")]
    header += [p(name, "cellc") for _, name, _ in criteria]
    rows = [header]
    for f in data.finalists:
        ratings = {r.code: r for r in f.criteria}
        rank = (str(f.rank) + (" (tie)" if f.is_tie else "")) if f.rank else "—"
        cells = []
        for code, _, _ in criteria:
            value = ratings[code].snapshot_rating
            cells.append(
                p(
                    ("—" if value is None else str(value)) + ("*" if code in f.unreviewed else ""),
                    "cellc",
                )
            )
        rows.append(
            [
                p(rank, "cell"),
                markup(
                    f"<b>{escape(f.name)}</b><br/><font color='#5b6876'>"
                    f"{escape(f.organisation or '')}</font>",
                    "cell",
                ),
                p(f.score_display or "—", "cellc"),
                p(f.points_display or "—", "cellc"),
            ]
            + cells
        )
    width = landscape(A4)[0] - 32 * mm
    fixed = 16 * mm + 55 * mm + 16 * mm + 16 * mm
    per = max((width - fixed) / max(len(criteria), 1), 18 * mm)
    disputed = [
        str(f.claim_counts.get("disputed", 0) + f.claim_counts.get("missing", 0))
        for f in data.finalists
    ]
    story = [
        p(data.category, "title"),
        p("Category comparison report", "subtitle"),
        *_status_notice(data),
        p("Provisional ranking", "h2"),
        grid(rows, [16 * mm, 55 * mm, 16 * mm, 16 * mm] + [per] * len(criteria)),
        p(
            "Ranked by full-precision score; ties remain provisional pending the committee's "
            "tie-break procedure. * rating not yet reviewed. — not rated or rejected (no score).",
            "small",
        ),
        p("Close results and ties", "h2"),
        *bullets(
            [f"{a} and {b}: {gap} apart" for a, b, gap in data.close_results],
            "No finalists are within 0.25 of each other.",
        ),
        p("Criteria driving the differences", "h2"),
        *bullets(
            [
                f"{s.criterion}: {', '.join(s.highest)} {s.high} vs "
                f"{', '.join(s.lowest)} {s.low} (spread {s.size})"
                for s in data.spreads
                if s.size > 0
            ],
            "All finalists received the same rating on every criterion.",
        ),
        p("Unresolved verification", "h2"),
        grid(
            [
                [
                    p("Finalist", "cell"),
                    p("Ratings not reviewed", "cellc"),
                    p("Disputed / missing claims", "cellc"),
                    p("Unresolved contradictions", "cellc"),
                    p("Unverified claims", "cellc"),
                ]
            ]
            + [
                [
                    p(f.name, "cell"),
                    p(str(len(f.unreviewed)), "cellc"),
                    p(d, "cellc"),
                    p(str(len(f.unresolved_contradictions)), "cellc"),
                    p(str(f.claim_counts.get("unverified", 0)), "cellc"),
                ]
                for f, d in zip(data.finalists, disputed, strict=True)
            ],
            [70 * mm, None, None, None, None],
        ),
        p("Questions for judges", "h2"),
        *bullets(data.questions, "No open questions arise from the record."),
        p("Finalist summaries", "h2"),
    ]
    for f in data.finalists:
        story += [
            p(f.name, "h3"),
            p("Strengths: " + ("; ".join(f.strengths) or "none rated 4 or above")),
            p("Limitations: " + ("; ".join(f.limitations) or "none"), "small"),
        ]
    story += _provenance(data, None)
    return _build(story, data, f"Category report: {data.category}", landscape(A4))
