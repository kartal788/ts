"""
uyeler_routes.py
================
Yönetici paneli – Üyeler sayfası route'ları.

Eklenmesi gereken route'lar (main.py'ye ekleyin):
  from Backend.fastapi.routes.uyeler_routes import (
      admin_uyeler_page,
      admin_uye_detay_page,
      admin_uye_stream_history_api,
  )

  @app.get("/admin/uyeler", response_class=HTMLResponse)
  async def admin_uyeler(request: Request, _: bool = Depends(require_auth)):
      return await admin_uyeler_page(request)

  @app.get("/admin/uyeler/{member_id}", response_class=HTMLResponse)
  async def admin_uye_detay(member_id: str, request: Request, _: bool = Depends(require_auth)):
      return await admin_uye_detay_page(request, member_id)

  @app.get("/api/admin/uyeler/{member_id}/streams")
  async def admin_uye_streams(member_id: str, _: bool = Depends(require_auth)):
      return await admin_uye_stream_history_api(member_id)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import Request, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from Backend import db
from Backend.config import Telegram
from Backend.fastapi.themes import get_theme, get_all_themes
from Backend.fastapi.security.credentials import get_current_user

templates = Jinja2Templates(directory="Backend/fastapi/templates")


# ─── Yardımcı: Byte → GB ─────────────────────────────────────────────────────

def _bytes_to_gb(b: int) -> float:
    return round(b / 1_073_741_824, 3) if b else 0.0


# ─── Yardımcı: Tüm üyeleri birleştir (token + subscriber) ───────────────────

async def _build_members_list() -> list[dict]:
    """
    Token tablosunu ve subscriber tablosunu birleştirerek tek bir
    liste döner. Her kayıt şunları içerir:
      user_id, user_name, token, subscription_status, subscription_expiry,
      total_bytes, daily_bytes, monthly_bytes, limits
    """
    try:
        tokens      = await db.get_all_api_tokens()
        subscribers = await db.get_all_subscribers()
    except Exception as e:
        return []

    # subscriber_map: user_id (str) → user doc
    subscriber_map: dict = {}
    for u in subscribers:
        uid = str(u.get("_id", ""))
        if uid:
            subscriber_map[uid] = u

    now = datetime.utcnow()
    result: list[dict] = []
    seen_user_ids: set = set()

    for t in tokens:
        token_str     = t.get("token", "")
        token_user_id = t.get("user_id")
        usage         = t.get("usage", {})

        # Kullanıcı dokümanını bul
        user = None
        if token_user_id:
            uid_str = str(token_user_id)
            user    = subscriber_map.get(uid_str)
            if not user:
                try:
                    user = await db.get_user(int(token_user_id))
                except Exception:
                    pass
            seen_user_ids.add(uid_str)

        # İsim çöz
        name = t.get("name") or ""
        if user:
            name = user.get("first_name") or user.get("username") or name
        if not name:
            name = f"Kullanıcı {token_user_id}" if token_user_id else f"Token …{token_str[-6:]}"

        # Abonelik durumu
        sub_status = None
        expiry     = None
        if user:
            sub_status = user.get("subscription_status")
            expiry     = user.get("subscription_expiry")
        if not expiry:
            expiry = t.get("subscription_expiry") or t.get("expires_at")

        if sub_status is None:
            if Telegram.SUBSCRIPTION:
                if not user:
                    sub_status = "expired"
                else:
                    sub_status = user.get("subscription_status", "expired")
            else:
                sub_status = "active" if (not expiry or (expiry and expiry > now)) else "expired"

        result.append({
            "user_id":              token_user_id,
            "user_name":            name,
            "token":                token_str,
            "subscription_status":  sub_status,
            "subscription_expiry":  expiry.isoformat() if isinstance(expiry, datetime) else expiry,
            "total_bytes":          usage.get("total_bytes", 0),
            "daily_bytes":          usage.get("daily", {}).get("bytes", 0),
            "monthly_bytes":        usage.get("monthly", {}).get("bytes", 0),
            "limits":               t.get("limits"),
        })

    # Token'sız aboneleri de ekle
    for uid_str, u in subscriber_map.items():
        if uid_str in seen_user_ids:
            continue
        sub_status = u.get("subscription_status", "expired")
        expiry     = u.get("subscription_expiry")
        name       = u.get("first_name") or u.get("username") or f"Kullanıcı {uid_str}"
        result.append({
            "user_id":             u.get("_id"),
            "user_name":           name,
            "token":               None,
            "subscription_status": sub_status,
            "subscription_expiry": expiry.isoformat() if isinstance(expiry, datetime) else None,
            "total_bytes":         0,
            "daily_bytes":         0,
            "monthly_bytes":       0,
            "limits":              None,
        })

    # Toplam veriye göre sırala
    result.sort(key=lambda x: x["total_bytes"], reverse=True)
    return result


# ─── Yardımcı: Owner adı ─────────────────────────────────────────────────────

async def _get_owner_name() -> str:
    """OWNER_ID'ye kayıtlı Telegram kullanıcısının adını döner."""
    try:
        owner_doc = await db.get_user(Telegram.OWNER_ID)
        if owner_doc and owner_doc.get("first_name"):
            return owner_doc["first_name"]
    except Exception:
        pass
    return ""


# ─── Sayfa: /admin/uyeler ────────────────────────────────────────────────────

async def admin_uyeler_page(request: Request) -> HTMLResponse:
    theme_name = request.session.get("theme", "purple_gradient")
    theme      = get_theme(theme_name)
    current_user = get_current_user(request)
    owner_name   = await _get_owner_name()

    return templates.TemplateResponse("uyeler.html", {
        "request":       request,
        "theme":         theme,
        "themes":        get_all_themes(),
        "current_theme": theme_name,
        "app_name":      Telegram.ISIM,
        "current_user":  current_user,
        "owner_name":    owner_name,
    })


# ─── Sayfa: /admin/uyeler/{member_id} ────────────────────────────────────────

async def admin_uye_detay_page(request: Request, member_id: str) -> HTMLResponse:
    theme_name = request.session.get("theme", "purple_gradient")
    theme      = get_theme(theme_name)
    current_user = get_current_user(request)
    owner_name   = await _get_owner_name()

    return templates.TemplateResponse("uye_detay.html", {
        "request":       request,
        "theme":         theme,
        "themes":        get_all_themes(),
        "current_theme": theme_name,
        "app_name":      Telegram.ISIM,
        "current_user":  current_user,
        "owner_name":    owner_name,
        "member_id":     member_id,
    })


# ─── API: /api/admin/uyeler (üye listesi) ────────────────────────────────────

async def admin_uyeler_list_api() -> dict:
    """
    Tüm üyeleri token ve subscriber tablosunu birleştirerek döner.
    Veri kullanımına göre sıralı.
    """
    try:
        members = await _build_members_list()
        return {"status": "success", "members": members, "total": len(members)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─── API: /api/admin/uyeler/{member_id}/streams ──────────────────────────────

async def admin_uye_stream_history_api(member_id: str) -> dict:
    """
    Belirli bir üyenin stream geçmişini ve istatistiklerini döner.
    member_id: user_id (sayı) veya token string olabilir.
    """
    try:
        col = db.dbs["tracking"]["stream_analytics"]

        # Kullanıcıya ait token'ı bul
        user_token: Optional[str] = None

        # Önce token olarak dene
        token_doc = await db.dbs["tracking"]["api_tokens"].find_one(
            {"token": member_id}
        )
        if token_doc:
            user_token = member_id
        else:
            # user_id olarak dene
            try:
                uid = int(member_id)
                token_doc = await db.dbs["tracking"]["api_tokens"].find_one(
                    {"user_id": uid}
                )
                if token_doc:
                    user_token = token_doc.get("token")
            except (ValueError, TypeError):
                pass

        # Sorgu filtresi
        if user_token:
            match_filter = {"user_token": user_token}
        else:
            # Hiç token bulamazsak boş dön
            return {
                "status":   "success",
                "streams":  [],
                "summary":  {},
                "top_content": [],
            }

        # ── Tüm stream kayıtları (en yeni önce) ──────────────────────────────
        from pymongo import DESCENDING
        cursor = col.find(
            match_filter,
            {
                "_id":          0,
                "stream_id":    1,
                "title":        1,
                "imdb_id":      1,
                "total_bytes":  1,
                "duration_sec": 1,
                "avg_mbps":     1,
                "peak_mbps":    1,
                "status":           1,
                "logged_at":        1,
                "client_index":     1,
                "dc_id":            1,
                "certification_tr": 1,
                "certification_de": 1,
                "certification_us": 1,
            }
        ).sort("logged_at", DESCENDING).limit(500)
        streams_raw = await cursor.to_list(None)

        streams = []
        for s in streams_raw:
            logged = s.get("logged_at")
            streams.append({
                "stream_id":    s.get("stream_id"),
                "title":        s.get("title"),
                "imdb_id":      s.get("imdb_id"),
                "total_bytes":  s.get("total_bytes", 0),
                "duration_sec": s.get("duration_sec", 0),
                "avg_mbps":     round(s.get("avg_mbps", 0), 3),
                "peak_mbps":    round(s.get("peak_mbps", 0), 3),
                "status":           s.get("status"),
                "logged_at":        logged.isoformat() if isinstance(logged, datetime) else logged,
                "client_index":     s.get("client_index"),
                "dc_id":            s.get("dc_id"),
                "certification_tr": s.get("certification_tr"),
                "certification_de": s.get("certification_de"),
                "certification_us": s.get("certification_us"),
            })

        # ── Özet istatistikler ────────────────────────────────────────────────
        from pymongo import ASCENDING
        agg_pipe = [
            {"$match": match_filter},
            {"$group": {
                "_id":            None,
                "total_streams":  {"$sum": 1},
                "total_bytes":    {"$sum": "$total_bytes"},
                "avg_speed":      {"$avg": "$avg_mbps"},
                "peak_speed":     {"$max": "$peak_mbps"},
                "avg_duration":   {"$avg": "$duration_sec"},
                "total_duration": {"$sum": "$duration_sec"},
            }},
        ]
        agg_result = await col.aggregate(agg_pipe).to_list(1)
        summary    = agg_result[0] if agg_result else {}
        summary.pop("_id", None)

        # ── En çok izlenen içerikler ──────────────────────────────────────────
        top_pipe = [
            {"$match": {**match_filter, "title": {"$ne": None, "$exists": True}}},
            {"$group": {
                "_id":                "$title",
                "imdb_id":            {"$first": "$imdb_id"},
                "watch_count":        {"$sum": 1},
                "total_bytes":        {"$sum": "$total_bytes"},
                "last_watched":       {"$max": "$logged_at"},
                "certification_tr":   {"$first": "$certification_tr"},
                "certification_de":   {"$first": "$certification_de"},
                "certification_us":   {"$first": "$certification_us"},
            }},
            {"$sort": {"watch_count": -1}},
            {"$limit": 10},
        ]
        top_raw     = await col.aggregate(top_pipe).to_list(None)
        top_content = []
        for r in top_raw:
            lw = r.get("last_watched")
            top_content.append({
                "title":              r["_id"],
                "imdb_id":            r.get("imdb_id"),
                "watch_count":        r["watch_count"],
                "total_bytes":        r["total_bytes"],
                "last_watched":       lw.isoformat() if isinstance(lw, datetime) else lw,
                "certification_tr":   r.get("certification_tr"),
                "certification_de":   r.get("certification_de"),
                "certification_us":   r.get("certification_us"),
            })

        return {
            "status":      "success",
            "streams":     streams,
            "summary":     summary,
            "top_content": top_content,
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
