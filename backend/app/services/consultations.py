"""首页滚动咨询消息服务。"""

from __future__ import annotations

from urllib.parse import urlparse

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.exceptions import BadRequestError
from app.db.models import ConsultationMessage

MAX_MESSAGES = 10
MAX_CONTENT_CHARS = 160


class ConsultationService:
    def list_active(self, db: Session) -> dict:
        rows = db.execute(
            select(ConsultationMessage)
            .where(ConsultationMessage.is_active.is_(True))
            .order_by(ConsultationMessage.sort_order.asc(), ConsultationMessage.id.asc())
        ).scalars().all()
        updated = max((row.updated_at for row in rows if row.updated_at), default=None)
        return {
            "items": [
                {"id": row.id, "content": row.content, "sort_order": row.sort_order or 0}
                | {"url": row.url or ""}
                for row in rows
            ],
            "updated_at": updated.strftime("%Y-%m-%d %H:%M") if updated else "",
        }

    def replace_all(self, db: Session, contents: list) -> dict:
        cleaned: list[tuple[str, str]] = []
        seen: set[str] = set()
        for raw in contents:
            if isinstance(raw, str):
                text_value, url_value = raw, ""
            elif isinstance(raw, dict):
                text_value, url_value = raw.get("content", ""), raw.get("url", "")
            else:
                text_value, url_value = getattr(raw, "content", ""), getattr(raw, "url", "")
            text = " ".join(str(text_value or "").split())
            if not text:
                continue
            if len(text) > MAX_CONTENT_CHARS:
                raise BadRequestError(
                    f"每条咨询不超过 {MAX_CONTENT_CHARS} 字",
                    detail={"code": "CONSULTATION_TOO_LONG"},
                )
            url = str(url_value or "").strip()
            if url:
                parsed = urlparse(url)
                if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                    raise BadRequestError(
                        "咨询链接必须是 http 或 https 地址",
                        detail={"code": "CONSULTATION_URL_INVALID"},
                    )
            if text in seen:
                continue
            seen.add(text)
            cleaned.append((text, url))
        if len(cleaned) > MAX_MESSAGES:
            raise BadRequestError(
                f"咨询消息最多 {MAX_MESSAGES} 条",
                detail={"code": "CONSULTATION_TOO_MANY"},
            )
        db.execute(delete(ConsultationMessage))
        for index, (content, url) in enumerate(cleaned):
            db.add(ConsultationMessage(content=content, url=url, sort_order=index, is_active=True))
        db.commit()
        return self.list_active(db)


_service = ConsultationService()


def get_consultation_service() -> ConsultationService:
    return _service
