"""
webpush.py
==========
Yönetici panelinin (istekler.html vb.) açık olduğu tarayıcılara, yeni bir
içerik talebi veya abonelik talebi geldiğinde tarayıcı Web Push bildirimi
gönderir (Telegram bildiriminden bağımsız, ayrı bir kanal).

Akış:
  1. Yönetici, İstekler sayfasında bildirimlere izin verir → tarayıcı bir
     PushSubscription üretir → bu abonelik /api/admin/push/abone-ol ile
     DB'ye kaydedilir (Database.add_push_subscription).
  2. Yeni içerik/abonelik talebi oluştuğunda notify_admins(...) çağrılır →
     kayıtlı tüm aboneliklere VAPID imzalı push mesajı gönderilir.
  3. Tarayıcıdaki service worker (sw.js) push event'ini yakalayıp sistem
     bildirimi olarak gösterir.

pywebpush kurulu değilse (opsiyonel bağımlılık) bu modül sessizce devre dışı
kalır; Telegram bildirimleri ve site işlevselliği bundan etkilenmez.
"""

from __future__ import annotations

import asyncio
import json
import logging

from Backend import db

_logger = logging.getLogger(__name__)

try:
    from pywebpush import webpush, WebPushException
    _PYWEBPUSH_AVAILABLE = True
except Exception:  # pragma: no cover - opsiyonel bağımlılık kurulu değilse
    _PYWEBPUSH_AVAILABLE = False


async def notify_admins(title: str, body: str, url: str = "/istekler", tag: str = "istek") -> None:
    """
    Kayıtlı tüm yönetici Push aboneliklerine bildirim gönderir.
    Geçersiz/süresi dolmuş abonelikler (404/410) otomatik olarak silinir.
    """
    if not _PYWEBPUSH_AVAILABLE:
        return

    try:
        vapid = await db.get_or_create_vapid_keys()
        subs = await db.list_push_subscriptions()
    except Exception as e:
        _logger.warning("Web push için VAPID anahtarları/abonelikler okunamadı: %s", e)
        return

    if not subs:
        return

    payload = json.dumps({"title": title, "body": body, "url": url, "tag": tag})

    async def _send_one(sub_doc: dict) -> None:
        subscription_info = sub_doc.get("subscription")
        endpoint = sub_doc.get("endpoint")
        if not subscription_info:
            return
        try:
            await asyncio.to_thread(
                webpush,
                subscription_info=subscription_info,
                data=payload,
                vapid_private_key=vapid["private_key"],
                vapid_claims={"sub": "mailto:admin@example.com"},
            )
        except WebPushException as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status in (404, 410):
                # Abonelik artık geçerli değil (tarayıcı kapatılmış/izin kaldırılmış)
                await db.remove_push_subscription(endpoint)
            else:
                _logger.warning("Web push gönderilemedi (%s): %s", endpoint, e)
        except Exception as e:
            _logger.warning("Web push gönderilemedi (beklenmeyen hata, %s): %s", endpoint, e)

    await asyncio.gather(*[_send_one(s) for s in subs], return_exceptions=True)
