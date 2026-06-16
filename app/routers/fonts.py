from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import EthiopicFont
from app.schemas import FontListOut, FontOut

router = APIRouter(tags=["Fonts"])


@router.get("/fonts", response_model=FontListOut)
async def list_fonts(
    request: Request,
    q: Optional[str] = Query(None, description="Search family, display name, style, or file name"),
    active_only: bool = Query(True),
    ethiopic_only: bool = Query(True),
    format: Optional[str] = Query(None, description="Filter by ttf, otf, woff, or woff2"),
    page: int = Query(1, ge=1),
    limit: int = Query(100, ge=1, le=250),
    db: AsyncSession = Depends(get_db),
):
    """List available presentation fonts with file/CSS URLs."""
    conditions = _font_conditions(q=q, active_only=active_only, ethiopic_only=ethiopic_only, font_format=format)

    count_stmt = select(func.count()).select_from(EthiopicFont)
    if conditions:
        count_stmt = count_stmt.where(*conditions)
    total = (await db.execute(count_stmt)).scalar() or 0

    stmt = (
        select(EthiopicFont)
        .order_by(EthiopicFont.family_name, EthiopicFont.weight, EthiopicFont.style_name, EthiopicFont.display_name)
        .offset((page - 1) * limit)
        .limit(limit)
    )
    if conditions:
        stmt = stmt.where(*conditions)

    fonts = (await db.execute(stmt)).scalars().all()
    return {
        "total": total,
        "page": page,
        "page_size": limit,
        "fonts": [_font_out(font, request) for font in fonts],
    }


@router.get("/fonts.css")
async def fonts_css(
    request: Request,
    slugs: Optional[str] = Query(None, description="Comma-separated font slugs. Defaults to all active Ethiopic fonts."),
    active_only: bool = Query(True),
    ethiopic_only: bool = Query(True),
    db: AsyncSession = Depends(get_db),
):
    """Return @font-face declarations for selected fonts."""
    stmt = select(EthiopicFont).order_by(EthiopicFont.family_name, EthiopicFont.weight, EthiopicFont.style_name)
    conditions = _font_conditions(q=None, active_only=active_only, ethiopic_only=ethiopic_only, font_format=None)

    requested_slugs = _parse_slugs(slugs)
    if requested_slugs:
        conditions.append(EthiopicFont.slug.in_(requested_slugs))
    if conditions:
        stmt = stmt.where(*conditions)

    fonts = (await db.execute(stmt)).scalars().all()
    if requested_slugs:
        found = {font.slug for font in fonts}
        missing = sorted(set(requested_slugs) - found)
        if missing:
            raise HTTPException(status_code=404, detail=f"Font not found: {', '.join(missing)}")

    css = "\n\n".join(_font_face_css(font, request) for font in fonts)
    etag = _collection_etag(fonts)
    headers = _css_headers(etag)
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(css + ("\n" if css else ""), media_type="text/css; charset=utf-8", headers=headers)


@router.get("/fonts/{slug}", response_model=FontOut)
async def get_font(slug: str, request: Request, db: AsyncSession = Depends(get_db)):
    """Get metadata for one font."""
    font = await _get_font_or_404(slug, db)
    return _font_out(font, request)


@router.get("/fonts/{slug}/css")
async def get_font_css(slug: str, request: Request, db: AsyncSession = Depends(get_db)):
    """Return one @font-face rule."""
    font = await _get_font_or_404(slug, db)
    etag = f'"css-{font.sha256}"'
    headers = _css_headers(etag)
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(_font_face_css(font, request) + "\n", media_type="text/css; charset=utf-8", headers=headers)


@router.get("/fonts/{slug}/file")
async def get_font_file(slug: str, request: Request, db: AsyncSession = Depends(get_db)):
    """Download the raw font file for browser/app rendering."""
    font = await _get_font_or_404(slug, db)
    etag = f'"{font.sha256}"'
    headers = {
        "Cache-Control": "public, max-age=3600",
        "ETag": etag,
        "Content-Disposition": f'inline; filename="{_safe_header_filename(font.file_name)}"',
        "X-Font-Family": font.family_name,
        "X-Font-Style": font.style_name,
        "X-Font-Weight": str(font.weight),
    }
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(font.data, media_type=font.mime_type, headers=headers)


async def _get_font_or_404(slug: str, db: AsyncSession) -> EthiopicFont:
    result = await db.execute(select(EthiopicFont).where(EthiopicFont.slug == slug))
    font = result.scalar_one_or_none()
    if not font or not font.is_active:
        raise HTTPException(status_code=404, detail=f"Font '{slug}' not found")
    return font


def _font_conditions(
    *,
    q: Optional[str],
    active_only: bool,
    ethiopic_only: bool,
    font_format: Optional[str],
) -> list:
    conditions = []
    if active_only:
        conditions.append(EthiopicFont.is_active == True)
    if ethiopic_only:
        conditions.append(EthiopicFont.supports_ethiopic == True)
    if font_format:
        conditions.append(EthiopicFont.format == font_format.lower().strip("."))
    if q:
        term = f"%{q.strip()}%"
        conditions.append(or_(
            EthiopicFont.family_name.ilike(term),
            EthiopicFont.display_name.ilike(term),
            EthiopicFont.style_name.ilike(term),
            EthiopicFont.file_name.ilike(term),
        ))
    return conditions


def _font_out(font: EthiopicFont, request: Request) -> dict:
    return {
        "slug": font.slug,
        "family_name": font.family_name,
        "display_name": font.display_name,
        "style_name": font.style_name,
        "weight": font.weight,
        "is_italic": font.is_italic,
        "format": font.format,
        "mime_type": font.mime_type,
        "file_name": font.file_name,
        "file_size": font.file_size,
        "sha256": font.sha256,
        "supports_ethiopic": font.supports_ethiopic,
        "license_name": font.license_name,
        "license_url": font.license_url,
        "source": font.source,
        "is_active": font.is_active,
        "css_url": _absolute_url(request, "get_font_css", slug=font.slug),
        "file_url": _absolute_url(request, "get_font_file", slug=font.slug),
    }


def _font_face_css(font: EthiopicFont, request: Request) -> str:
    family = _css_string(font.family_name)
    url = _absolute_url(request, "get_font_file", slug=font.slug)
    return (
        "@font-face {\n"
        f"  font-family: {family};\n"
        f"  font-style: {'italic' if font.is_italic else 'normal'};\n"
        f"  font-weight: {font.weight};\n"
        "  font-display: swap;\n"
        f"  src: url('{url}') format('{_css_format(font.format)}');\n"
        "}\n"
    )


def _css_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _css_format(font_format: str) -> str:
    return {
        "ttf": "truetype",
        "otf": "opentype",
        "woff": "woff",
        "woff2": "woff2",
    }.get(font_format, font_format)


def _parse_slugs(value: Optional[str]) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def _collection_etag(fonts: list[EthiopicFont]) -> str:
    digest = "-".join(font.sha256[:12] for font in fonts)
    return f'"fonts-{len(fonts)}-{digest}"'


def _css_headers(etag: str) -> dict[str, str]:
    return {
        "Cache-Control": "public, max-age=300",
        "ETag": etag,
    }


def _safe_header_filename(value: str) -> str:
    return value.replace("\\", "_").replace('"', "_").replace("\n", "_").replace("\r", "_")


def _absolute_url(request: Request, route_name: str, **path_params) -> str:
    path = request.app.url_path_for(route_name, **path_params)
    host = (
        request.headers.get("x-forwarded-host")
        or request.headers.get("host")
        or request.url.netloc
    )
    proto = (
        request.headers.get("x-forwarded-proto")
        or request.headers.get("x-scheme")
        or request.url.scheme
        or "https"
    ).split(",")[0].strip()
    if host.endswith(".up.railway.app"):
        proto = "https"
    return f"{proto}://{host}{path}"
