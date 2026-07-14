from math import ceil
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session


ALLOWED_PAGE_SIZES = (10, 25, 50, 100)


def normalize_page(page: int = 1, page_size: int = 25) -> tuple[int, int]:
    page = max(page or 1, 1)
    page_size = page_size if page_size in ALLOWED_PAGE_SIZES else 25
    return page, page_size


def paginate(db: Session, stmt: Select, *, page: int = 1, page_size: int = 25) -> tuple[list[Any], dict[str, Any]]:
    page, page_size = normalize_page(page, page_size)
    total_items = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
    total_pages = ceil(total_items / page_size) if total_items else 0
    if total_pages and page > total_pages:
        page = total_pages
    offset = (page - 1) * page_size
    items = db.scalars(stmt.limit(page_size).offset(offset)).all()
    return items, {
        "page": page,
        "page_size": page_size,
        "total_items": total_items,
        "total_pages": total_pages,
        "has_next": page < total_pages,
        "has_previous": page > 1,
        "start_item": offset + 1 if total_items else 0,
        "end_item": min(offset + page_size, total_items),
        "allowed_page_sizes": ALLOWED_PAGE_SIZES,
    }
