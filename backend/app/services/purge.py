"""Secure deletion of a finalist's data (Act s.16, s.18(4)-(5); data protection review §4.5).

Everything held about one nomination is deleted in a single transaction: documents and their
extracted text and embeddings, claims and reviews, public-source findings, AI and human
ratings, ranking entries, calibration items, consents, and reports that contain the finalist.
Audit records about them are kept as events but their details are replaced with a redaction
marker, so the log still shows *that* things happened without saying *what* about whom.

Stored files are deleted after the transaction commits. Each file was encrypted with its own
data key held only in the file's header, so deleting the file destroys the key and the
ciphertext cannot be recovered ("crypto-shredding"). Copies in backups expire with the backups.

The immutability rules are bypassed only inside this transaction, via SET LOCAL wob.purge.
"""

import json
import uuid
from datetime import UTC, date, datetime

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models import AwardCategory, FinalistCategory, User
from app.services import audit
from app.services.storage import FileStorage

# (table, column) deletions, in foreign-key order, keyed on ids gathered first.
_STEPS = [
    ("calibration_ratings", "item_id", "calibration_items"),
    ("calibration_items", "id", "calibration_items"),
    ("assessment_citations", "criterion_assessment_id", "criterion_assessments"),
    ("criterion_assessments", "id", "criterion_assessments"),
    ("ranking_entries", "id", "ranking_entries"),
    ("reports", "id", "reports"),
    ("claim_evidence", "claim_id", "claims"),
    ("verification_reviews", "claim_id", "claims"),
    ("claims", "id", "claims"),
    ("contradiction_resolutions", "extraction_run_id", "claim_extraction_runs"),
    ("claim_extraction_runs", "id", "claim_extraction_runs"),
    ("external_sources", "id", "external_sources"),
    ("research_runs", "id", "research_runs"),
    ("document_chunks", "document_id", "documents"),
    ("documents", "id", "documents"),
    ("imports", "id", "imports"),
    ("finalist_consents", "id", "finalist_consents"),
]

_BY_FINALIST = [
    "calibration_items",
    "criterion_assessments",
    "ranking_entries",
    "claims",
    "claim_extraction_runs",
    "external_sources",
    "research_runs",
    "documents",
    "imports",
    "finalist_consents",
]
_GATHER = {
    table: f"SELECT id FROM {table} WHERE finalist_category_id = :fc"  # noqa: S608 (fixed names)
    for table in _BY_FINALIST
} | {
    # Category reports name every finalist, so they go too (they can be regenerated).
    "reports": "SELECT id FROM reports WHERE finalist_category_id = :fc "
    "OR (kind = 'category' AND category_id = :category)",
}


def purge_finalist(
    session: Session,
    storage: FileStorage,
    finalist_category_id: uuid.UUID,
    actor: User | None,
    reason: str,
) -> dict[str, int]:
    """Delete everything about one nomination. Commits; files are shredded after commit."""
    fc = session.get(FinalistCategory, finalist_category_id)
    if fc is None:
        raise LookupError("finalist not found in this category")
    finalist_id, category_id = fc.finalist_id, fc.category_id
    session.execute(text("SET LOCAL wob.purge = 'on'"))

    params = {"fc": fc.id, "category": category_id}
    ids = {name: list(session.scalars(text(sql), params)) for name, sql in _GATHER.items()}
    storage_keys = list(
        session.scalars(
            text(
                "SELECT storage_key FROM documents WHERE finalist_category_id = :fc "
                "UNION ALL SELECT storage_key FROM reports WHERE id = ANY(:reports) "
                "AND storage_key IS NOT NULL"
            ),
            {"fc": fc.id, "reports": ids["reports"]},
        )
    )

    counts: dict[str, int] = {}
    for table, column, source in _STEPS:
        result = session.execute(
            text(f"DELETE FROM {table} WHERE {column} = ANY(:ids)"),  # noqa: S608 (fixed names)
            {"ids": ids[source]},
        )
        counts[table] = result.rowcount

    redacted_ids = [fc.id] + [i for values in ids.values() for i in values]
    stamp = {"redacted": f"personal data purged {date.today().isoformat()}"}
    counts["audit_events_redacted"] = session.execute(
        text(
            "UPDATE audit_events SET details = CAST(:stamp AS jsonb) "
            "WHERE entity_id = ANY(:ids) OR details::text LIKE :fc_text"
        ),
        {"stamp": json.dumps(stamp), "ids": redacted_ids, "fc_text": f"%{fc.id}%"},
    ).rowcount

    session.execute(text("DELETE FROM finalist_categories WHERE id = :fc"), {"fc": fc.id})
    remaining = session.scalar(
        text("SELECT count(*) FROM finalist_categories WHERE finalist_id = :f"), {"f": finalist_id}
    )
    if not remaining:
        session.execute(text("DELETE FROM finalists WHERE id = :f"), {"f": finalist_id})
        counts["finalist_records"] = 1

    audit.record(
        session,
        actor,
        "data.purged",
        "finalist_category",
        fc.id,
        category_id=str(category_id),
        reason=reason,
        counts=counts,
    )
    session.commit()

    for key in storage_keys:
        storage.delete(key)
    counts["files_shredded"] = len(storage_keys)
    return counts


def purge_category(
    session: Session,
    storage: FileStorage,
    category_id: uuid.UUID,
    actor: User | None,
    reason: str,
) -> dict[str, int]:
    totals: dict[str, int] = {"finalists": 0}
    for fc_id in list(
        session.scalars(
            select(FinalistCategory.id).where(FinalistCategory.category_id == category_id)
        )
    ):
        counts = purge_finalist(session, storage, fc_id, actor, reason)
        totals["finalists"] += 1
        for key, value in counts.items():
            totals[key] = totals.get(key, 0) + value
    return totals


def purge_expired(
    session: Session, storage: FileStorage, today: date | None = None
) -> dict[str, dict[str, int]]:
    """Purge every category whose retention date has passed (s.18(4)). Run daily."""
    today = today or datetime.now(UTC).date()
    results = {}
    for category in session.scalars(
        select(AwardCategory).where(AwardCategory.retention_until < today)
    ).all():
        results[category.name] = purge_category(
            session,
            storage,
            category.id,
            None,
            f"retention period ended {category.retention_until.isoformat()}",
        )
    return results
