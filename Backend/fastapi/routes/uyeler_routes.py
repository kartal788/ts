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

  @app.get("/api/admin/uyarilar")
  async def admin_uyarilar(_: bool = Depends(require_auth)):
      return await admin_usage_discrepancies_api()

  @app.get("/api/admin/uyeler/{member_id}/subscription-history")
  async def admin_uye_subscription_history(member_id: str, _: bool = Depends(require_auth)):
      return await admin_uye_subscription_history_api(member_id)

  @app.post("/api/admin/uyeler/{member_id}/ban")
  async def admin_uye_ban(member_id: str, _: bool = Depends(require_auth)):
      return await admin_uye_ban_api(member_id, True)

  @app.post("/api/admin/uyeler/{member_id}/unban")
  async def admin_uye_unban(member_id: str, _: bool = Depends(require_auth)):
      return await admin_uye_ban_api(member_id, False)

  @app.post("/api/admin/uyeler/{member_id}/clear-devices")
  async def admin_uye_clear_devices(member_id: str, _: bool = Depends(require_auth)):
      return await admin_uye_clear_devices_api(member_id)
"""

from __future__ import annotations

import logging
_logger = logging.getLogger(__name__)

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

        is_banned = bool(user and user.get("banned"))
        if is_banned:
            sub_status = "banned"

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
            "is_banned":            is_banned,
        })

    # Token'sız aboneleri de ekle
    for uid_str, u in subscriber_map.items():
        if uid_str in seen_user_ids:
            continue
        sub_status = u.get("subscription_status", "expired")
        expiry     = u.get("subscription_expiry")
        name       = u.get("first_name") or u.get("username") or f"Kullanıcı {uid_str}"
        is_banned  = bool(u.get("banned"))
        if is_banned:
            sub_status = "banned"
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
            "is_banned":           is_banned,
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
        _logger.error("Internal error", exc_info=True)

        raise HTTPException(status_code=500, detail="Sunucu hatası")


# ─── API: /api/admin/uyarilar (Bugün / İzleme geçmişi GB tutarsızlığı) ──────

async def admin_usage_discrepancies_api() -> dict:
    """
    Dashboard'daki "Uyarılar" kartı için tüm uyarı türlerini toplayıp döner:
      - usage_discrepancy:     "Bugün" sayacı ile izleme geçmişi arasında GB farkı olanlar
      - daily_limit:           Günlük veri limiti dolan üyeler
      - pending_request:       İçerik talebi yapıp onaylanmayan üyeler
      - pending_subscription:  Abonelik planı seçip aboneliği/ödemesi onaylanmayan üyeler
      - expiring_soon:         Aboneliği 24 saat içinde sona erecek üyeler
      - expired_but_active:    Aboneliği sona ermiş ama hâlâ "active" işaretli üyeler
    """
    try:
        discrepancy_rows = await db.get_daily_usage_discrepancies()
        alerts = [{
            "type":          "usage_discrepancy",
            "user_id":       r.get("user_id"),
            "name":          r.get("name"),
            "token":         (r.get("token") or "")[:8] + "..." if r.get("token") else None,
            "history_gb":    _bytes_to_gb(r.get("history_bytes", 0)),
            "daily_gb":      _bytes_to_gb(r.get("daily_bytes", 0)),
            "diff_gb":       _bytes_to_gb(r.get("diff_bytes", 0)),
        } for r in discrepancy_rows]

        daily_limit_rows = await db.get_daily_limit_reached_tokens()
        daily_limit_alerts = [{
            "type":             "daily_limit",
            "user_id":          r.get("user_id"),
            "name":             r.get("name"),
            "token":            (r.get("token") or "")[:8] + "..." if r.get("token") else None,
            "daily_used_gb":    _bytes_to_gb(r.get("daily_used_bytes", 0)),
            "daily_limit_gb":   r.get("daily_limit_gb", 0),
        } for r in daily_limit_rows]

        pending_request_rows = await db.get_pending_content_request_members()
        pending_request_alerts = [{
            "type":               "pending_request",
            "user_id":            r.get("user_id"),
            "name":               r.get("name"),
            "pending_count":      r.get("pending_count", 0),
            "last_title":         r.get("last_title"),
            "last_media_type":    r.get("last_media_type"),
            "first_requested_at": r.get("first_requested_at"),
            "last_requested_at":  r.get("last_requested_at"),
        } for r in pending_request_rows]

        pending_subscription_rows = await db.get_pending_subscription_payments()
        pending_subscription_alerts = [{
            "type":          "pending_subscription",
            "user_id":       r.get("user_id"),
            "name":          r.get("name"),
            "plan_label":    r.get("plan_label"),
            "duration_days": r.get("duration_days", 0),
            "price":         r.get("price", 0),
            "currency":      r.get("currency", "TRY"),
            "requested_at":  r.get("requested_at"),
        } for r in pending_subscription_rows]

        expiring_soon_rows = await db.get_expiring_soon_alerts(hours=24)
        expiring_soon_alerts = [{
            "type":            "expiring_soon",
            "user_id":         r.get("user_id"),
            "name":            r.get("name"),
            "expires_at":      r.get("expires_at"),
            "hours_remaining": r.get("hours_remaining"),
        } for r in expiring_soon_rows]

        expired_but_active_rows = await db.get_expired_but_active_alerts()
        expired_but_active_alerts = [{
            "type":          "expired_but_active",
            "user_id":       r.get("user_id"),
            "name":          r.get("name"),
            "expired_at":    r.get("expired_at"),
            "overdue_hours": r.get("overdue_hours"),
        } for r in expired_but_active_rows]

        total = (
            len(alerts) + len(daily_limit_alerts)
            + len(pending_request_alerts) + len(pending_subscription_alerts)
            + len(expiring_soon_alerts) + len(expired_but_active_alerts)
        )

        return {
            "status": "success",
            "alerts": alerts,
            "daily_limit_alerts": daily_limit_alerts,
            "pending_request_alerts": pending_request_alerts,
            "pending_subscription_alerts": pending_subscription_alerts,
            "expiring_soon_alerts": expiring_soon_alerts,
            "expired_but_active_alerts": expired_but_active_alerts,
            "total": total,
        }
    except Exception as e:
        _logger.error("Internal error", exc_info=True)
        raise HTTPException(status_code=500, detail="Sunucu hatası")


# ─── Yardımcı: member_id'yi token / user_id / token_doc'a çöz ────────────────

async def _resolve_member(member_id: str) -> dict:
    """
    member_id: token string veya Telegram user_id (sayı) olabilir.
    Döner: {"user_token": str|None, "token_doc": dict|None, "user_id": int|None}
    """
    token_doc = await db.dbs["tracking"]["api_tokens"].find_one({"token": member_id})
    user_token: Optional[str] = None
    user_id: Optional[int] = None

    if token_doc:
        user_token = member_id
        user_id = token_doc.get("user_id")
    else:
        try:
            uid = int(member_id)
            user_id = uid
            token_doc = await db.dbs["tracking"]["api_tokens"].find_one({"user_id": uid})
            if token_doc:
                user_token = token_doc.get("token")
        except (ValueError, TypeError):
            pass

    return {"user_token": user_token, "token_doc": token_doc, "user_id": user_id}


# ─── API: /api/admin/uyeler/{member_id}/streams ──────────────────────────────

async def admin_uye_stream_history_api(member_id: str) -> dict:
    """
    Belirli bir üyenin stream geçmişini, istatistiklerini, cihaz/limit bilgisini,
    limit aşım geçmişini ve diğer üyelere göre karşılaştırmasını döner.
    member_id: user_id (sayı) veya token string olabilir.
    """
    try:
        col = db.dbs["tracking"]["stream_analytics"]

        resolved    = await _resolve_member(member_id)
        user_token  = resolved["user_token"]
        token_doc   = resolved["token_doc"]
        user_id     = resolved["user_id"]

        # ── Cihaz / Oturum / Limit bilgisi (token varsa) ─────────────────────
        device_info: dict = {}
        limits: dict = (token_doc or {}).get("limits", {}) or {}
        if token_doc:
            usage = token_doc.get("usage", {}) or {}
            active_count = await db.get_active_device_count(user_token) if user_token else 0
            expires_at = token_doc.get("expires_at")
            device_info = {
                "active_devices":       active_count,
                "device_limit":         limits.get("device_limit", 0),
                "ip_limit":             limits.get("ip_limit", 0),
                "daily_limit_gb":       limits.get("daily_limit_gb", 0),
                "monthly_limit_gb":     limits.get("monthly_limit_gb", 0),
                "speed_limit_mbps":     limits.get("speed_limit_mbps", 0),
                "daily_used_bytes":     usage.get("daily", {}).get("bytes", 0),
                "monthly_used_bytes":   usage.get("monthly", {}).get("bytes", 0),
                "total_used_bytes":     usage.get("total_bytes", 0),
                "daily_limit_warned":   bool(token_doc.get("daily_limit_warned")),
                "daily_limit_finished": bool(token_doc.get("daily_limit_finished")),
                "daily_limit_disabled": bool(token_doc.get("daily_limit_disabled")),
                "token_expires_at":     expires_at.isoformat() if isinstance(expires_at, datetime) else expires_at,
            }

        # Sorgu filtresi
        if not user_token:
            # Hiç token bulamazsak, geçmiş boş ama diğer alanlar (karşılaştırma vb.) yine de dönsün
            comparison = await _build_comparison(user_id=user_id, user_token=None, my_bytes=0)
            return {
                "status":            "success",
                "streams":           [],
                "summary":           {},
                "top_content":       [],
                "daily_video_usage": [],
                "device_info":       device_info,
                "limit_overrun_days": [],
                "comparison":        comparison,
                "user_id":           user_id,
                "user_token":        user_token,
            }

        match_filter = {"user_token": user_token}

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
        ).sort("logged_at", DESCENDING).limit(1000)
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

        # ── Günlük bazda, hangi video kaç GB veri çekmiş ────────────────────────
        # Not: "day" alanı Europe/Istanbul (UTC+3) takvim gününe göre hesaplanır,
        # yoksa Mongo varsayılan olarak UTC gün sınırını kullanır ve saat 00:00-03:00
        # (İstanbul) arasındaki izlemeler bir önceki güne yazılır.
        daily_video_pipe = [
            {"$match": {**match_filter, "title": {"$ne": None, "$exists": True}, "logged_at": {"$ne": None, "$exists": True}}},
            {"$group": {
                "_id": {
                    "day":   {"$dateToString": {"format": "%Y-%m-%d", "date": "$logged_at", "timezone": "Europe/Istanbul"}},
                    "title": "$title",
                },
                "imdb_id":     {"$first": "$imdb_id"},
                "watch_count": {"$sum": 1},
                "total_bytes": {"$sum": "$total_bytes"},
            }},
            {"$sort": {"_id.day": -1, "total_bytes": -1}},
            {"$limit": 1000},
        ]
        daily_video_raw = await col.aggregate(daily_video_pipe).to_list(None)
        daily_video_usage = []
        for r in daily_video_raw:
            _id = r.get("_id", {})
            daily_video_usage.append({
                "day":         _id.get("day"),
                "title":       _id.get("title"),
                "imdb_id":     r.get("imdb_id"),
                "watch_count": r.get("watch_count", 0),
                "total_bytes": r.get("total_bytes", 0),
            })

        # ── Limit Aşım Geçmişi ────────────────────────────────────────────────
        # Not: Geçmiş limit değerleri saklanmadığından, mevcut günlük limit
        # eşiği geçmiş günlere de uygulanarak yaklaşık bir aşım listesi çıkarılır.
        limit_overrun_days: list = []
        daily_limit_bytes = float(limits.get("daily_limit_gb") or 0) * 1_073_741_824
        if daily_limit_bytes > 0:
            day_totals: dict = {}
            for r in daily_video_usage:
                d = r.get("day")
                if not d:
                    continue
                day_totals[d] = day_totals.get(d, 0) + (r.get("total_bytes") or 0)
            for day, total in day_totals.items():
                if total > daily_limit_bytes:
                    limit_overrun_days.append({
                        "day":          day,
                        "total_bytes":  total,
                        "limit_bytes":  daily_limit_bytes,
                        "over_pct":     round((total / daily_limit_bytes - 1) * 100, 1),
                    })
            limit_overrun_days.sort(key=lambda x: x["day"], reverse=True)
            limit_overrun_days = limit_overrun_days[:60]

        # ── Karşılaştırma & Bağlam (diğer üyelere göre, aylık veriye göre) ───
        my_bytes = device_info.get("monthly_used_bytes", 0) if device_info else 0
        comparison = await _build_comparison(user_id=user_id, user_token=user_token, my_bytes=my_bytes)

        return {
            "status":             "success",
            "streams":            streams,
            "summary":            summary,
            "top_content":        top_content,
            "daily_video_usage":  daily_video_usage,
            "device_info":        device_info,
            "limit_overrun_days": limit_overrun_days,
            "comparison":         comparison,
            "user_id":            user_id,
            "user_token":         user_token,
        }

    except Exception as e:
        _logger.error("Internal error", exc_info=True)

        raise HTTPException(status_code=500, detail="Sunucu hatası")


# ─── Yardımcı: Diğer üyelere göre sıralama / karşılaştırma ───────────────────

async def _build_comparison(user_id: Optional[int], user_token: Optional[str], my_bytes: float) -> dict:
    """
    Üyeyi diğer üyelerle AYLIK veri kullanımına göre karşılaştırır.
    `my_bytes` çağıran tarafından aylık kullanım (bytes) olarak verilmelidir.
    """
    try:
        all_members = await _build_members_list()
        if not all_members:
            return {}

        # Aylık kullanıma göre büyükten küçüğe sırala (genel üye listesi
        # toplam veriye göre sıralı geldiği için burada ayrıca sıralıyoruz)
        ranked_members = sorted(all_members, key=lambda m: m.get("monthly_bytes", 0) or 0, reverse=True)

        monthly_values = [m.get("monthly_bytes", 0) or 0 for m in ranked_members]
        avg_bytes = sum(monthly_values) / len(monthly_values) if monthly_values else 0

        rank = None
        for idx, m in enumerate(ranked_members, start=1):
            if user_token and m.get("token") == user_token:
                rank = idx
                break
            if user_id is not None and m.get("user_id") == user_id:
                rank = idx
                break

        return {
            "rank":            rank,
            "total_members":   len(ranked_members),
            "avg_total_bytes": avg_bytes,
            "percent_vs_avg":  round(((my_bytes - avg_bytes) / avg_bytes) * 100, 1) if avg_bytes > 0 else None,
        }
    except Exception:
        return {}


# ─── API: /api/admin/uyeler/{member_id}/reminders ────────────────────────────

async def admin_uye_reminders_api(member_id: str) -> dict:
    """
    Belirli bir üyenin dizi ve film hatırlatmalarını döner.
    member_id: user_id (sayı) veya token string olabilir.

    main.py'ye eklenecek route:
      @app.get("/api/admin/uyeler/{member_id}/reminders")
      async def admin_uye_reminders(member_id: str, _: bool = Depends(require_auth)):
          return await admin_uye_reminders_api(member_id)
    """
    try:
        # Kullanıcının user_id'sini çöz
        user_id: Optional[int] = None

        # Önce doğrudan sayı mı dene
        try:
            user_id = int(member_id)
        except (ValueError, TypeError):
            pass

        # Sayı değilse token olarak ara
        if user_id is None:
            token_doc = await db.dbs["tracking"]["api_tokens"].find_one(
                {"token": member_id}, {"_id": 0, "user_id": 1}
            )
            if token_doc:
                user_id = token_doc.get("user_id")

        if user_id is None:
            return {"status": "success", "tv": [], "movie": []}

        # Dizi hatırlatmaları
        tv_col = db.dbs["tracking"]["tv_reminders"]
        tv_cursor = tv_col.find(
            {"user_ids": user_id},
            {"_id": 0, "tmdb_id": 1, "db_index": 1, "title": 1, "poster": 1, "status": 1}
        )
        tv_items = await tv_cursor.to_list(length=200)

        # Film hatırlatmaları
        movie_col = db.dbs["tracking"]["movie_reminders"]
        movie_cursor = movie_col.find(
            {"user_ids": user_id},
            {"_id": 0, "tmdb_id": 1, "db_index": 1, "title": 1, "poster": 1, "status": 1}
        )
        movie_items = await movie_cursor.to_list(length=200)

        return {
            "status": "success",
            "tv":     tv_items,
            "movie":  movie_items,
        }

    except Exception as e:
        _logger.error("admin_uye_reminders_api error", exc_info=True)
        raise HTTPException(status_code=500, detail="Sunucu hatası")


# ─── API: /api/admin/uyeler/{member_id}/subscription-history ────────────────

async def admin_uye_subscription_history_api(member_id: str) -> dict:
    """
    Belirli bir üyenin abonelik/ödeme geçmişini (onaylar, admin uzatma/azaltma/
    iptal işlemleri) döner.

    main.py'ye eklenecek route:
      @app.get("/api/admin/uyeler/{member_id}/subscription-history")
      async def admin_uye_subscription_history(member_id: str, _: bool = Depends(require_auth)):
          return await admin_uye_subscription_history_api(member_id)
    """
    try:
        resolved = await _resolve_member(member_id)
        user_id  = resolved["user_id"]
        if user_id is None:
            return {"status": "success", "history": []}

        history = await db.get_subscription_history(user_id)
        return {"status": "success", "history": history}
    except Exception:
        _logger.error("admin_uye_subscription_history_api error", exc_info=True)
        raise HTTPException(status_code=500, detail="Sunucu hatası")


# ─── API: /api/admin/uyeler/{member_id}/ban ve /unban ────────────────────────

async def admin_uye_ban_api(member_id: str, ban: bool) -> dict:
    """
    Belirli bir üyeyi banlar/ban kaldırır. Yalnızca Telegram user_id'si çözülebilen
    üyeler için çalışır (token'sız/subscriber kaydı olmayan üyeler banlanamaz).

    main.py'ye eklenecek route'lar:
      @app.post("/api/admin/uyeler/{member_id}/ban")
      async def admin_uye_ban(member_id: str, _: bool = Depends(require_auth)):
          return await admin_uye_ban_api(member_id, True)

      @app.post("/api/admin/uyeler/{member_id}/unban")
      async def admin_uye_unban(member_id: str, _: bool = Depends(require_auth)):
          return await admin_uye_ban_api(member_id, False)
    """
    try:
        resolved = await _resolve_member(member_id)
        user_id  = resolved["user_id"]
        if user_id is None:
            raise HTTPException(status_code=404, detail="Üyenin Telegram ID'si bulunamadı")

        if ban:
            await db.ban_user(user_id)
        else:
            await db.unban_user(user_id)

        return {"status": "success", "banned": ban}
    except HTTPException:
        raise
    except Exception:
        _logger.error("admin_uye_ban_api error", exc_info=True)
        raise HTTPException(status_code=500, detail="Sunucu hatası")


# ─── API: /api/admin/uyeler/{member_id}/clear-devices ────────────────────────

async def admin_uye_clear_devices_api(member_id: str) -> dict:
    """
    Belirli bir üyenin aktif cihaz/oturum kayıtlarını temizler (token'daki
    active_devices listesi sıfırlanır — cihaz limiti aşımını manuel çözmek için).

    main.py'ye eklenecek route:
      @app.post("/api/admin/uyeler/{member_id}/clear-devices")
      async def admin_uye_clear_devices(member_id: str, _: bool = Depends(require_auth)):
          return await admin_uye_clear_devices_api(member_id)
    """
    try:
        resolved   = await _resolve_member(member_id)
        user_token = resolved["user_token"]
        if not user_token:
            raise HTTPException(status_code=404, detail="Üyeye ait token bulunamadı")

        await db.clear_device_sessions(user_token)
        return {"status": "success"}
    except HTTPException:
        raise
    except Exception:
        _logger.error("admin_uye_clear_devices_api error", exc_info=True)
        raise HTTPException(status_code=500, detail="Sunucu hatası")
