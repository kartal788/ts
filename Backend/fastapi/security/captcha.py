"""
captcha.py
==========
Sunucu taraflı resim seçme CAPTCHA modülü.

Nasıl çalışır (session-free, HMAC token tabanlı):
  1. set_captcha(session)  ->  CaptchaData döner
     - Doğru index'ler HMAC-SHA256 ile imzalanmış bir token'a yazılır
     - token, CaptchaData.token alanında template'e geçer
     - token bir hidden input olarak form içinde taşınır (session'a gerek yok)
  2. Kullanıcı resimleri seçer, index listesi (JSON) + token POST ile gelir
  3. verify_captcha(session, selected_json, token) token'ı doğrular
     - Token 10 dakika geçerlidir (replay saldırısına karşı)
     - Session tamamen opsiyoneldir (geriye dönük uyumluluk için tutuldu)

Resimler: Tamamen Python'da üretilen inline SVG -> base64 data URI.
Dış bağımlılık, dosya sistemi veya CDN gerektirmez.

Kategoriler: hayvan (kedi, köpek, kuş, balık), araç (araba, bisiklet, uçak, gemi),
             meyve (elma, muz, çilek, üzüm)
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import random
import time
from dataclasses import dataclass, field
from typing import List

CAPTCHA_SESSION_KEY = "img_captcha"  # geriye dönük uyumluluk
_TOKEN_TTL = 600  # 10 dakika

# HMAC secret — uygulama başlarken bir kez üretilir, process ömrü boyunca sabit
import secrets as _secrets
_HMAC_SECRET: bytes = _secrets.token_bytes(32)

# ---------------------------------------------------------------------------
# SVG tanımları — 80x80 px, beyaz/açık arka plan, renkli basit çizimler
# ---------------------------------------------------------------------------
_SVGS: dict[str, str] = {

    # ── HAYVANLAR ──────────────────────────────────────────────────────────
    "kedi": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 80 80">
<rect width="80" height="80" rx="10" fill="#FFF8F0"/>
<ellipse cx="40" cy="52" rx="20" ry="16" fill="#E8A87C"/>
<circle cx="40" cy="28" r="14" fill="#E8A87C"/>
<polygon points="24,18 20,6 32,14" fill="#E8A87C"/>
<polygon points="25,17 22,9 30,15" fill="#F4C2A1"/>
<polygon points="56,18 60,6 48,14" fill="#E8A87C"/>
<polygon points="55,17 58,9 50,15" fill="#F4C2A1"/>
<ellipse cx="34" cy="27" rx="4" ry="5" fill="white"/>
<ellipse cx="46" cy="27" rx="4" ry="5" fill="white"/>
<circle cx="34" cy="28" r="2.5" fill="#2C2C2C"/>
<circle cx="46" cy="28" r="2.5" fill="#2C2C2C"/>
<circle cx="35" cy="27" r="1" fill="white"/>
<circle cx="47" cy="27" r="1" fill="white"/>
<polygon points="40,33 38,36 42,36" fill="#D4607A"/>
<path d="M38,36 Q40,39 42,36" stroke="#2C2C2C" stroke-width="1" fill="none"/>
<line x1="42" y1="34" x2="56" y2="32" stroke="#888" stroke-width="1"/>
<line x1="42" y1="35" x2="56" y2="36" stroke="#888" stroke-width="1"/>
<line x1="38" y1="34" x2="24" y2="32" stroke="#888" stroke-width="1"/>
<line x1="38" y1="35" x2="24" y2="36" stroke="#888" stroke-width="1"/>
<path d="M58,60 Q70,50 65,40" stroke="#E8A87C" stroke-width="5" fill="none" stroke-linecap="round"/>
<ellipse cx="30" cy="66" rx="7" ry="5" fill="#E8A87C"/>
<ellipse cx="50" cy="66" rx="7" ry="5" fill="#E8A87C"/>
</svg>""",

    "köpek": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 80 80">
<rect width="80" height="80" rx="10" fill="#F0F8FF"/>
<ellipse cx="40" cy="54" rx="22" ry="16" fill="#C8A46E"/>
<circle cx="40" cy="28" r="15" fill="#C8A46E"/>
<ellipse cx="24" cy="32" rx="7" ry="11" fill="#A0784A" transform="rotate(-15,24,32)"/>
<ellipse cx="56" cy="32" rx="7" ry="11" fill="#A0784A" transform="rotate(15,56,32)"/>
<ellipse cx="40" cy="35" rx="9" ry="7" fill="#E8C49A"/>
<circle cx="34" cy="26" r="4" fill="white"/>
<circle cx="46" cy="26" r="4" fill="white"/>
<circle cx="34" cy="27" r="2.5" fill="#3C2C1A"/>
<circle cx="46" cy="27" r="2.5" fill="#3C2C1A"/>
<circle cx="35" cy="26" r="1" fill="white"/>
<circle cx="47" cy="26" r="1" fill="white"/>
<ellipse cx="40" cy="34" rx="4" ry="3" fill="#3C2C1A"/>
<path d="M37,37 Q40,41 43,37" stroke="#3C2C1A" stroke-width="1.5" fill="none"/>
<ellipse cx="40" cy="40" rx="3" ry="4" fill="#E8607A"/>
<path d="M60,52 Q72,44 68,36" stroke="#C8A46E" stroke-width="6" fill="none" stroke-linecap="round"/>
<rect x="28" y="66" width="8" height="10" rx="4" fill="#C8A46E"/>
<rect x="44" y="66" width="8" height="10" rx="4" fill="#C8A46E"/>
</svg>""",

    "kuş": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 80 80">
<rect width="80" height="80" rx="10" fill="#F0FFF4"/>
<ellipse cx="40" cy="48" rx="18" ry="14" fill="#5BA85A"/>
<ellipse cx="24" cy="50" rx="10" ry="7" fill="#4A9248" transform="rotate(-20,24,50)"/>
<ellipse cx="56" cy="50" rx="10" ry="7" fill="#4A9248" transform="rotate(20,56,50)"/>
<circle cx="40" cy="28" r="13" fill="#5BA85A"/>
<circle cx="34" cy="26" r="4" fill="white"/>
<circle cx="34" cy="26" r="2.5" fill="#1A1A1A"/>
<circle cx="35" cy="25" r="1" fill="white"/>
<circle cx="46" cy="26" r="4" fill="white"/>
<circle cx="46" cy="26" r="2.5" fill="#1A1A1A"/>
<circle cx="47" cy="25" r="1" fill="white"/>
<polygon points="40,32 34,36 46,36" fill="#F4A020"/>
<path d="M40,16 Q36,8 38,4 Q40,10 42,4 Q44,8 40,16" fill="#F4A020"/>
<line x1="35" y1="60" x2="32" y2="72" stroke="#F4A020" stroke-width="2.5"/>
<line x1="45" y1="60" x2="48" y2="72" stroke="#F4A020" stroke-width="2.5"/>
<line x1="32" y1="72" x2="26" y2="70" stroke="#F4A020" stroke-width="2"/>
<line x1="32" y1="72" x2="32" y2="76" stroke="#F4A020" stroke-width="2"/>
<line x1="48" y1="72" x2="54" y2="70" stroke="#F4A020" stroke-width="2"/>
<line x1="48" y1="72" x2="48" y2="76" stroke="#F4A020" stroke-width="2"/>
</svg>""",

    "balık": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 80 80">
<rect width="80" height="80" rx="10" fill="#E8F4FF"/>
<polygon points="14,40 24,28 24,52" fill="#2E86C1"/>
<ellipse cx="48" cy="40" rx="24" ry="16" fill="#3498DB"/>
<ellipse cx="48" cy="44" rx="20" ry="10" fill="#85C1E9"/>
<path d="M36,24 Q44,16 56,24" stroke="#2E86C1" stroke-width="3" fill="#2E86C1" opacity="0.7"/>
<polygon points="42,54 48,62 54,54" fill="#2E86C1" opacity="0.7"/>
<circle cx="62" cy="37" r="5" fill="white"/>
<circle cx="62" cy="37" r="3" fill="#1A1A1A"/>
<circle cx="63" cy="36" r="1.2" fill="white"/>
<path d="M70,40 Q74,38 72,36" stroke="#1A5276" stroke-width="1.5" fill="none"/>
<path d="M52,28 Q54,40 52,52" stroke="#2E86C1" stroke-width="2" fill="none"/>
<ellipse cx="44" cy="38" rx="4" ry="3" fill="none" stroke="#2980B9" stroke-width="1" opacity="0.5"/>
<ellipse cx="52" cy="36" rx="4" ry="3" fill="none" stroke="#2980B9" stroke-width="1" opacity="0.5"/>
</svg>""",

    # ── ARAÇLAR ────────────────────────────────────────────────────────────
    "araba": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 80 80">
<rect width="80" height="80" rx="10" fill="#F5F5F5"/>
<rect x="8" y="44" width="64" height="20" rx="5" fill="#E74C3C"/>
<path d="M18,44 L24,26 L56,26 L62,44 Z" fill="#C0392B"/>
<path d="M28,43 L32,29 L50,29 L54,43 Z" fill="#AED6F1" opacity="0.8"/>
<rect x="22" y="30" width="10" height="12" rx="2" fill="#AED6F1" opacity="0.8"/>
<rect x="48" y="30" width="10" height="12" rx="2" fill="#AED6F1" opacity="0.8"/>
<circle cx="22" cy="64" r="10" fill="#2C2C2C"/>
<circle cx="22" cy="64" r="6" fill="#888"/>
<circle cx="22" cy="64" r="3" fill="#2C2C2C"/>
<circle cx="58" cy="64" r="10" fill="#2C2C2C"/>
<circle cx="58" cy="64" r="6" fill="#888"/>
<circle cx="58" cy="64" r="3" fill="#2C2C2C"/>
<ellipse cx="68" cy="50" rx="4" ry="3" fill="#F9E547"/>
<ellipse cx="12" cy="50" rx="4" ry="3" fill="#E74C3C"/>
<line x1="40" y1="44" x2="40" y2="62" stroke="#C0392B" stroke-width="1.5"/>
<rect x="31" y="51" width="6" height="2" rx="1" fill="#BDC3C7"/>
<rect x="43" y="51" width="6" height="2" rx="1" fill="#BDC3C7"/>
</svg>""",

    "bisiklet": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 80 80">
<rect width="80" height="80" rx="10" fill="#FAFAFA"/>
<circle cx="20" cy="54" r="16" fill="none" stroke="#2C2C2C" stroke-width="3"/>
<circle cx="20" cy="54" r="4" fill="#2C2C2C"/>
<circle cx="60" cy="54" r="16" fill="none" stroke="#2C2C2C" stroke-width="3"/>
<circle cx="60" cy="54" r="4" fill="#2C2C2C"/>
<line x1="20" y1="38" x2="20" y2="70" stroke="#888" stroke-width="1"/>
<line x1="4" y1="54" x2="36" y2="54" stroke="#888" stroke-width="1"/>
<line x1="9" y1="43" x2="31" y2="65" stroke="#888" stroke-width="1"/>
<line x1="9" y1="65" x2="31" y2="43" stroke="#888" stroke-width="1"/>
<line x1="60" y1="38" x2="60" y2="70" stroke="#888" stroke-width="1"/>
<line x1="44" y1="54" x2="76" y2="54" stroke="#888" stroke-width="1"/>
<line x1="49" y1="43" x2="71" y2="65" stroke="#888" stroke-width="1"/>
<line x1="49" y1="65" x2="71" y2="43" stroke="#888" stroke-width="1"/>
<line x1="20" y1="54" x2="40" y2="30" stroke="#E74C3C" stroke-width="3"/>
<line x1="40" y1="30" x2="60" y2="54" stroke="#E74C3C" stroke-width="3"/>
<line x1="40" y1="30" x2="34" y2="54" stroke="#E74C3C" stroke-width="3"/>
<line x1="34" y1="54" x2="20" y2="54" stroke="#E74C3C" stroke-width="3"/>
<line x1="60" y1="54" x2="58" y2="30" stroke="#2C2C2C" stroke-width="3"/>
<line x1="52" y1="28" x2="64" y2="28" stroke="#2C2C2C" stroke-width="3"/>
<line x1="40" y1="30" x2="42" y2="20" stroke="#2C2C2C" stroke-width="2.5"/>
<ellipse cx="42" cy="19" rx="7" ry="3" fill="#2C2C2C"/>
<circle cx="34" cy="54" r="4" fill="none" stroke="#888" stroke-width="2"/>
</svg>""",

    "uçak": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 80 80">
<rect width="80" height="80" rx="10" fill="#EBF5FB"/>
<ellipse cx="40" cy="40" rx="30" ry="10" fill="#ECF0F1" transform="rotate(-20,40,40)"/>
<ellipse cx="58" cy="30" rx="8" ry="5" fill="#BDC3C7" transform="rotate(-20,58,30)"/>
<ellipse cx="36" cy="44" rx="8" ry="22" fill="#3498DB" transform="rotate(-20,36,44)"/>
<polygon points="18,54 14,42 24,50" fill="#3498DB"/>
<polygon points="16,50 10,44 24,48" fill="#2980B9"/>
<ellipse cx="42" cy="52" rx="6" ry="3" fill="#7F8C8D" transform="rotate(-20,42,52)"/>
<circle cx="52" cy="34" r="2.5" fill="#AED6F1"/>
<circle cx="46" cy="38" r="2.5" fill="#AED6F1"/>
<circle cx="40" cy="42" r="2.5" fill="#AED6F1"/>
<ellipse cx="60" cy="28" rx="5" ry="3" fill="#AED6F1" transform="rotate(-20,60,28)"/>
</svg>""",

    "gemi": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 80 80">
<rect width="80" height="80" rx="10" fill="#D6EAF8"/>
<path d="M4,58 Q20,54 40,58 Q60,62 76,58 L76,78 L4,78 Z" fill="#3498DB"/>
<path d="M12,58 L16,38 L64,38 L68,58 Z" fill="#E74C3C"/>
<rect x="14" y="34" width="52" height="6" rx="2" fill="#C0392B"/>
<rect x="28" y="22" width="24" height="14" rx="2" fill="#ECF0F1"/>
<rect x="31" y="25" width="6" height="5" rx="1" fill="#AED6F1"/>
<rect x="43" y="25" width="6" height="5" rx="1" fill="#AED6F1"/>
<rect x="38" y="10" width="8" height="14" rx="2" fill="#F39C12"/>
<circle cx="40" cy="8" r="3" fill="#BDC3C7" opacity="0.7"/>
<line x1="42" y1="10" x2="42" y2="4" stroke="#2C2C2C" stroke-width="1"/>
<polygon points="42,4 50,6 42,8" fill="#E74C3C"/>
<path d="M4,62 Q20,58 40,62 Q60,66 76,62" stroke="white" stroke-width="2" fill="none" opacity="0.6"/>
</svg>""",

    # ── MEYVELER ───────────────────────────────────────────────────────────
    "elma": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 80 80">
<rect width="80" height="80" rx="10" fill="#FFF5F5"/>
<ellipse cx="40" cy="68" rx="18" ry="4" fill="#DDD" opacity="0.5"/>
<path d="M40,18 Q62,18 64,42 Q66,62 40,66 Q14,62 16,42 Q18,18 40,18 Z" fill="#E74C3C"/>
<path d="M28,20 Q22,14 26,10 Q30,14 32,20 Z" fill="#E74C3C"/>
<path d="M52,20 Q58,14 54,10 Q50,14 48,20 Z" fill="#E74C3C"/>
<ellipse cx="30" cy="30" rx="6" ry="8" fill="white" opacity="0.25" transform="rotate(-20,30,30)"/>
<path d="M40,18 Q44,8 50,6" stroke="#5D4037" stroke-width="3" fill="none" stroke-linecap="round"/>
<path d="M46,10 Q54,6 56,14 Q50,16 46,10 Z" fill="#27AE60"/>
</svg>""",

    "muz": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 80 80">
<rect width="80" height="80" rx="10" fill="#FFFDE7"/>
<ellipse cx="42" cy="70" rx="20" ry="4" fill="#DDD" opacity="0.5"/>
<path d="M16,56 Q18,24 44,14 Q62,10 66,18 Q64,22 58,20 Q40,20 28,52 Q26,60 16,56 Z" fill="#F4D03F"/>
<path d="M16,56 Q18,24 44,14" stroke="#D4AC0D" stroke-width="2" fill="none"/>
<path d="M60,16 Q66,14 66,18 Q62,20 58,20 Z" fill="#A0522D"/>
<ellipse cx="18" cy="56" rx="5" ry="4" fill="#A0522D" transform="rotate(30,18,56)"/>
<path d="M26,48 Q32,28 50,18" stroke="white" stroke-width="3" fill="none" opacity="0.3" stroke-linecap="round"/>
</svg>""",

    "çilek": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 80 80">
<rect width="80" height="80" rx="10" fill="#FFF5F5"/>
<ellipse cx="40" cy="70" rx="16" ry="4" fill="#DDD" opacity="0.5"/>
<path d="M40,18 Q60,22 62,46 Q58,66 40,70 Q22,66 18,46 Q20,22 40,18 Z" fill="#E74C3C"/>
<ellipse cx="34" cy="36" rx="1.5" ry="2" fill="#F9EBEA"/>
<ellipse cx="42" cy="32" rx="1.5" ry="2" fill="#F9EBEA"/>
<ellipse cx="50" cy="38" rx="1.5" ry="2" fill="#F9EBEA"/>
<ellipse cx="46" cy="48" rx="1.5" ry="2" fill="#F9EBEA"/>
<ellipse cx="34" cy="52" rx="1.5" ry="2" fill="#F9EBEA"/>
<ellipse cx="38" cy="44" rx="1.5" ry="2" fill="#F9EBEA"/>
<ellipse cx="50" cy="56" rx="1.5" ry="2" fill="#F9EBEA"/>
<ellipse cx="32" cy="30" rx="5" ry="7" fill="white" opacity="0.2" transform="rotate(-15,32,30)"/>
<path d="M40,18 Q36,8 30,10 Q36,16 38,20 Z" fill="#27AE60"/>
<path d="M40,18 Q44,8 50,10 Q44,16 42,20 Z" fill="#27AE60"/>
<line x1="40" y1="14" x2="40" y2="6" stroke="#5D4037" stroke-width="2.5" stroke-linecap="round"/>
</svg>""",

    "üzüm": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 80 80">
<rect width="80" height="80" rx="10" fill="#F5EEF8"/>
<line x1="40" y1="16" x2="40" y2="8" stroke="#5D4037" stroke-width="3" stroke-linecap="round"/>
<path d="M40,12 Q48,6 52,12 Q48,20 40,12 Z" fill="#27AE60"/>
<circle cx="28" cy="26" r="8" fill="#8E44AD"/>
<circle cx="40" cy="24" r="8" fill="#9B59B6"/>
<circle cx="52" cy="26" r="8" fill="#8E44AD"/>
<circle cx="22" cy="40" r="8" fill="#9B59B6"/>
<circle cx="34" cy="38" r="8" fill="#8E44AD"/>
<circle cx="46" cy="38" r="8" fill="#9B59B6"/>
<circle cx="58" cy="40" r="8" fill="#8E44AD"/>
<circle cx="28" cy="54" r="8" fill="#8E44AD"/>
<circle cx="40" cy="56" r="8" fill="#9B59B6"/>
<circle cx="52" cy="54" r="8" fill="#8E44AD"/>
<circle cx="40" cy="68" r="8" fill="#9B59B6"/>
<circle cx="25" cy="23" r="2.5" fill="white" opacity="0.3"/>
<circle cx="37" cy="21" r="2.5" fill="white" opacity="0.3"/>
<circle cx="49" cy="23" r="2.5" fill="white" opacity="0.3"/>
</svg>""",
}

# Kategori -> resim adları
_CATEGORIES: dict[str, list[str]] = {
    "hayvan": ["kedi", "köpek", "kuş", "balık"],
    "araç":   ["araba", "bisiklet", "uçak", "gemi"],
    "meyve":  ["elma", "muz", "çilek", "üzüm"],
}

# Çok dilli kategori adları
_CAT_I18N: dict[str, dict[str, str]] = {
    "hayvan": {"tr": "hayvan",   "en": "animal",   "de": "Tier"},
    "araç":   {"tr": "araç",     "en": "vehicle",  "de": "Fahrzeug"},
    "meyve":  {"tr": "meyve",    "en": "fruit",    "de": "Frucht"},
}


@dataclass
class CaptchaData:
    """Template'e gönderilecek CAPTCHA verisi."""
    target_category: str
    target_i18n: dict
    images: list
    correct_count: int
    token: str = ""          # HMAC imzalı token — form hidden input olarak taşınır


def _to_b64(svg: str) -> str:
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()


def _make_token(correct_indices: list[int]) -> str:
    """
    Doğru index'leri + timestamp'i HMAC-SHA256 ile imzalar.
    Format: base64( json({"c": [...], "t": timestamp}) ) + "." + hex_hmac
    """
    payload = json.dumps({"c": sorted(correct_indices), "t": int(time.time())}, separators=(",", ":"))
    payload_b64 = base64.urlsafe_b64encode(payload.encode()).decode()
    sig = hmac.new(_HMAC_SECRET, payload_b64.encode(), hashlib.sha256).hexdigest()
    return f"{payload_b64}.{sig}"


def _verify_token(token: str, selected: set[int]) -> bool:
    """
    Token'ı doğrular:
    - İmza geçerli mi?
    - TTL (10 dk) aşılmadı mı?
    - Seçilen index'ler doğru mu?
    """
    try:
        payload_b64, sig = token.rsplit(".", 1)
    except ValueError:
        return False

    # İmza kontrolü (timing-safe)
    expected_sig = hmac.new(_HMAC_SECRET, payload_b64.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected_sig):
        return False

    # Payload çöz
    try:
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + "=="))
    except Exception:
        return False

    # TTL kontrolü
    if int(time.time()) - payload.get("t", 0) > _TOKEN_TTL:
        return False

    # Index kontrolü
    return set(payload.get("c", [])) == selected


def set_captcha(session: dict) -> CaptchaData:
    """
    Yeni resim CAPTCHA üretir.
    Doğru index'ler HMAC token'a yazılır (session artık kullanılmaz).
    Geriye dönük uyumluluk için session'a da yazar.
    """
    target_cat = random.choice(list(_CATEGORIES.keys()))
    correct_names = random.sample(_CATEGORIES[target_cat], 3)

    other_cats = [c for c in _CATEGORIES if c != target_cat]
    wrong_names: list[str] = []
    for cat in other_cats:
        wrong_names += random.sample(_CATEGORIES[cat], 3)

    all_items: list[tuple[str, bool]] = (
        [(n, True)  for n in correct_names] +
        [(n, False) for n in wrong_names]
    )
    random.shuffle(all_items)

    images = []
    correct_indices: list[int] = []
    for idx, (name, is_correct) in enumerate(all_items):
        images.append({"b64": _to_b64(_SVGS[name]), "index": idx})
        if is_correct:
            correct_indices.append(idx)

    # Session'a da yaz (eski kod çağırırsa çalışsın)
    session[CAPTCHA_SESSION_KEY] = {"correct": correct_indices}

    token = _make_token(correct_indices)

    return CaptchaData(
        target_category=target_cat,
        target_i18n=_CAT_I18N[target_cat],
        images=images,
        correct_count=3,
        token=token,
    )


def verify_captcha(session: dict, selected_json: str, token: str = "") -> bool:
    """
    Önce HMAC token ile doğrular (session-free, güvenilir).
    Token yoksa veya geçersizse session'a düşer (geriye dönük uyumluluk).
    """
    # Tip güvencesi: Form objesi veya None gelirse string'e çevir
    if not isinstance(selected_json, str):
        selected_json = ""
    if not isinstance(token, str):
        token = ""

    try:
        selected: set[int] = {int(i) for i in json.loads(selected_json)}
    except (ValueError, TypeError, json.JSONDecodeError):
        selected = set()

    # ── 1. HMAC token yolu (birincil) ──────────────────────────────────────
    if token:
        return _verify_token(token, selected)

    # ── 2. Session yolu (yedek — token gönderilmemişse) ────────────────────
    data = session.pop(CAPTCHA_SESSION_KEY, None)
    if data is None:
        return False
    expected: set[int] = set(data.get("correct", []))
    return selected == expected
