import asyncio
import logging
import json
import pathlib
from fastapi import Request, Query, HTTPException, UploadFile
from fastapi.responses import StreamingResponse, JSONResponse
from Backend import db, StartTime, __version__
from Backend.helper.pyro import get_readable_time
from Backend.pyrofork.bot import multi_clients, StreamBot
from Backend.helper.custom_dl import run_speed_test, _speed_test_single_client
from time import time

_logger = logging.getLogger(__name__)


# --- API Routes for System Stats ---

async def get_system_stats_api():
    try:
        db_stats = await db.get_database_stats()
        total_movies = sum(stat.get("movie_count", 0) for stat in db_stats)
        total_tv_shows = sum(stat.get("tv_count", 0) for stat in db_stats)
        api_tokens = await db.get_all_api_tokens()
        
        return {
            "server_status": "running",
            "uptime": get_readable_time(time() - StartTime),
            "telegram_bot": f"@{StreamBot.username}" if StreamBot and StreamBot.username else "@StreamBot",
            "connected_bots": len(multi_clients),
            "version": __version__,
            "movies": total_movies,
            "tv_shows": total_tv_shows,
            "databases": db_stats,
            "total_databases": len(db_stats),
            "current_db_index": db.current_db_index,
            "api_tokens": api_tokens
        }
    except Exception as e:
        _logger.error("System Stats API hatası", exc_info=True)
        return {
            "server_status": "error",
            "error": "İstatistikler yüklenemedi"
        }
    
# --- API Routes for Media Management ---

async def list_media_api(
    media_type: str = Query("movie", regex="^(movie|tv)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(24, ge=1, le=100),
    search: str = Query("", max_length=100)
):
    try:
        if search:
            result = await db.search_documents(search, page, page_size)
            filtered_results = [item for item in result['results'] if item.get('media_type') == media_type]
            total_filtered = len(filtered_results)
            start_index = (page - 1) * page_size
            end_index = start_index + page_size
            paged_results = filtered_results[start_index:end_index]
            
            return {
                "total_count": total_filtered,
                "current_page": page,
                "total_pages": (total_filtered + page_size - 1) // page_size,
                "movies" if media_type == "movie" else "tv_shows": paged_results
            }
        else:
            if media_type == "movie":
                return await db.sort_movies([], page, page_size)
            else:
                return await db.sort_tv_shows([], page, page_size)
    except Exception as e:
        _logger.error("Internal error", exc_info=True)

        raise HTTPException(status_code=500, detail="Sunucu hatası")

async def delete_media_api(
    tmdb_id: int,
    db_index: int,
    media_type: str = Query(regex="^(movie|tv)$")
):
    try:
        media_type_formatted = "Movie" if media_type == "movie" else "Series"
        result = await db.delete_document(media_type_formatted, tmdb_id, db_index)
        if result:
            return {"message": "Media deleted successfully"}
        else:
            raise HTTPException(status_code=404, detail="Media not found")
    except Exception as e:
        _logger.error("Internal error", exc_info=True)

        raise HTTPException(status_code=500, detail="Sunucu hatası")

async def update_media_api(
    request: Request,
    tmdb_id: int,
    db_index: int,
    media_type: str = Query(regex="^(movie|tv)$")
):
    try:
        update_data = await request.json()
        if 'rating' in update_data and update_data['rating']:
            try:
                update_data['rating'] = float(update_data['rating'])
            except (ValueError, TypeError):
                update_data['rating'] = 0.0
        
        if 'release_year' in update_data and update_data['release_year']:
            try:
                update_data['release_year'] = int(update_data['release_year'])
            except (ValueError, TypeError):
                pass
        if 'genres' in update_data:
            if isinstance(update_data['genres'], str):
                update_data['genres'] = [g.strip() for g in update_data['genres'].split(',') if g.strip()]
            elif not isinstance(update_data['genres'], list):
                update_data['genres'] = []

        if 'genres_tr' in update_data:
            if isinstance(update_data['genres_tr'], str):
                update_data['genres_tr'] = [g.strip() for g in update_data['genres_tr'].split(',') if g.strip()]
            elif not isinstance(update_data['genres_tr'], list):
                update_data['genres_tr'] = []

        if 'genres_de' in update_data:
            if isinstance(update_data['genres_de'], str):
                update_data['genres_de'] = [g.strip() for g in update_data['genres_de'].split(',') if g.strip()]
            elif not isinstance(update_data['genres_de'], list):
                update_data['genres_de'] = []

        if 'collection_id' in update_data:
            try:
                update_data['collection_id'] = int(update_data['collection_id']) if update_data['collection_id'] else None
            except (ValueError, TypeError):
                update_data['collection_id'] = None
        
        if 'languages' in update_data:
            if isinstance(update_data['languages'], str):
                update_data['languages'] = [l.strip() for l in update_data['languages'].split(',') if l.strip()]
            elif not isinstance(update_data['languages'], list):
                update_data['languages'] = []

        if 'original_language' in update_data:
            val = update_data['original_language']
            if isinstance(val, str):
                update_data['original_language'] = val.strip().lower() or None
            else:
                update_data['original_language'] = None
        if media_type == "movie":
            if 'runtime' in update_data and update_data['runtime']:
                try:
                    update_data['runtime'] = int(update_data['runtime'])
                except (ValueError, TypeError):
                    pass
        elif media_type == "tv":
            if 'total_seasons' in update_data and update_data['total_seasons']:
                try:
                    update_data['total_seasons'] = int(update_data['total_seasons'])
                except (ValueError, TypeError):
                    pass
            
            if 'total_episodes' in update_data and update_data['total_episodes']:
                try:
                    update_data['total_episodes'] = int(update_data['total_episodes'])
                except (ValueError, TypeError):
                    pass
        update_data = {k: v for k, v in update_data.items() if v != ""}
        result = await db.update_document(media_type, tmdb_id, db_index, update_data)
        if result:
            import asyncio
            from Backend.helper.platform_catalog import platform_catalog
            from Backend.helper.platform_catalog import platform_catalog as _pc
            _pc.schedule_refresh()
            return {"message": "Media updated successfully"}
        else:
            raise HTTPException(status_code=404, detail="Media not found or no changes made")

    except Exception as e:
        _logger.error("Internal error", exc_info=True)

        raise HTTPException(status_code=500, detail="Sunucu hatası")


# ─── API: İçerik görünürlüğü (kimler görebilir/erişebilir) ──────────────────
#
#   GET /api/media/visibility   → mevcut ayarı + seçim için üye listesini döner
#   PUT /api/media/visibility   → ayarı kaydeder
#
# main.py'ye eklenecek route'lar:
#   @app.get("/api/media/visibility")
#   async def get_media_visibility(tmdb_id: int, db_index: int, media_type: str = Query(regex="^(movie|tv)$"), _: bool = Depends(require_auth)):
#       return await get_media_visibility_api(tmdb_id, db_index, media_type)
#
#   @app.put("/api/media/visibility")
#   async def update_media_visibility(request: Request, tmdb_id: int, db_index: int, media_type: str = Query(regex="^(movie|tv)$"), _: bool = Depends(require_auth)):
#       return await update_media_visibility_api(request, tmdb_id, db_index, media_type)

async def get_media_visibility_api(
    tmdb_id: int,
    db_index: int,
    media_type: str = Query(regex="^(movie|tv)$"),
):
    try:
        media_doc = await db.get_document(media_type, tmdb_id, db_index)
        if not media_doc:
            raise HTTPException(status_code=404, detail="Media not found")

        visibility = await db.get_media_visibility(media_type, tmdb_id, db_index)

        # Seçim arayüzü için tüm üyeleri (token + subscriber birleşimi) döner.
        from Backend.fastapi.routes.uyeler_routes import _build_members_list
        members_raw = await _build_members_list()
        members = [
            {
                "user_id":             m.get("user_id"),
                "user_name":           m.get("user_name"),
                "subscription_status": m.get("subscription_status"),
            }
            for m in members_raw
            if m.get("user_id") is not None
        ]

        return {
            "status":     "success",
            "visibility": visibility,
            "members":    members,
        }
    except HTTPException:
        raise
    except Exception:
        _logger.error("get_media_visibility_api error", exc_info=True)
        raise HTTPException(status_code=500, detail="Sunucu hatası")


async def update_media_visibility_api(
    request: Request,
    tmdb_id: int,
    db_index: int,
    media_type: str = Query(regex="^(movie|tv)$"),
):
    try:
        media_doc = await db.get_document(media_type, tmdb_id, db_index)
        if not media_doc:
            raise HTTPException(status_code=404, detail="Media not found")

        payload = await request.json()
        mode = payload.get("mode")
        if mode not in ("subscribers", "selected"):
            raise HTTPException(status_code=400, detail="mode 'subscribers' veya 'selected' olmalı")

        member_ids = payload.get("member_ids") or []
        if not isinstance(member_ids, list):
            raise HTTPException(status_code=400, detail="member_ids bir liste olmalı")

        if mode == "selected" and not member_ids:
            raise HTTPException(
                status_code=400,
                detail="'Sadece seçtiğim üye(ler)' seçildiğinde en az bir üye seçilmelidir",
            )

        await db.save_media_visibility(media_type, tmdb_id, db_index, mode, member_ids)
        return {"status": "success"}
    except HTTPException:
        raise
    except Exception:
        _logger.error("update_media_visibility_api error", exc_info=True)
        raise HTTPException(status_code=500, detail="Sunucu hatası")


async def get_media_details_api(
    tmdb_id: int,
    db_index: int,
    media_type: str = Query(regex="^(movie|tv)$")
):
    try:
        result = await db.get_document(media_type, tmdb_id, db_index)
        if result:
            return result
        else:
            raise HTTPException(status_code=404, detail="Media not found")
    except Exception as e:
        _logger.error("Internal error", exc_info=True)

        raise HTTPException(status_code=500, detail="Sunucu hatası")

async def delete_movie_quality_api(tmdb_id: int, db_index: int, id: str):
    try:
        result = await db.delete_movie_quality(tmdb_id, db_index, id)
        if result:
            return {"message": "Quality deleted successfully"}
        else:
            raise HTTPException(status_code=404, detail="Quality not found")
    except Exception as e:
        _logger.error("Internal error", exc_info=True)

        raise HTTPException(status_code=500, detail="Sunucu hatası")

async def delete_tv_quality_api(
    tmdb_id: int, db_index: int, season: int, episode: int, id: str
):
    try:
        result = await db.delete_tv_quality(tmdb_id, db_index, season, episode, id)
        if result:
            return {"message": "deleted successfully"}
        else:
            raise HTTPException(status_code=404, detail="Quality not found")
    except Exception as e:
        _logger.error("Internal error", exc_info=True)

        raise HTTPException(status_code=500, detail="Sunucu hatası")

async def delete_tv_episode_api(
    tmdb_id: int, db_index: int, season: int, episode: int
):
    try:
        result = await db.delete_tv_episode(tmdb_id, db_index, season, episode)
        if result:
            return {"message": "Episode deleted successfully"}
        else:
            raise HTTPException(status_code=404, detail="Episode not found")
    except Exception as e:
        _logger.error("Internal error", exc_info=True)

        raise HTTPException(status_code=500, detail="Sunucu hatası")

async def delete_tv_season_api(tmdb_id: int, db_index: int, season: int):
    try:
        result = await db.delete_tv_season(tmdb_id, db_index, season)
        if result:
            return {"message": "Season deleted successfully"}
        else:
            raise HTTPException(status_code=404, detail="Season not found")
    except Exception as e:
        _logger.error("Internal error", exc_info=True)

        raise HTTPException(status_code=500, detail="Sunucu hatası")


# --- API Routes for Token Management ---

async def create_token_api(payload: dict):
    try:
        token_name = payload.get("name")
        daily_limit = payload.get("daily_limit_gb")
        monthly_limit = payload.get("monthly_limit_gb")
        speed_limit = payload.get("speed_limit_mbps")
        validity_days = payload.get("validity_days")
        telegram_user_id = payload.get("telegram_user_id")

        if not token_name:
             raise HTTPException(status_code=400, detail="Token name is required")
        def parse_limit(val):
            try:
                v = float(val)
                return v if v > 0 else None
            except (ValueError, TypeError):
                return None

        portal_username = payload.get("portal_username")
        portal_password = payload.get("portal_password")

        # validity_days → expires_at hesapla
        expires_at = None
        if validity_days:
            try:
                days = int(validity_days)
                if days > 0:
                    from datetime import datetime, timedelta
                    expires_at = datetime.utcnow() + timedelta(days=days)
            except (ValueError, TypeError):
                pass

        new_token = await db.add_api_token(
            token_name,
            parse_limit(daily_limit),
            parse_limit(monthly_limit),
            parse_limit(speed_limit),
            portal_username=portal_username.strip() if portal_username else None,
            portal_password=portal_password.strip() if portal_password else None,
            user_id=int(telegram_user_id) if telegram_user_id else None,
            expires_at=expires_at,
            validity_days=int(validity_days) if validity_days else None,
        )

        # Telegram ID girilmişse kullanıcıya abonelik kaydı oluştur/güncelle.
        # Bu sayede kullanıcı /uyelik komutunu çalıştırdığında aktif görünür.
        if telegram_user_id:
            try:
                tg_id = int(telegram_user_id)
                days_to_assign = int(validity_days) if validity_days else 36500  # validity_days yoksa "sınırsız" (100 yıl)
                await db.assign_subscription(tg_id, days_to_assign)
            except Exception as sub_err:
                # Abonelik kaydı hatası token oluşturmayı engellemesin, sadece logla
                from Backend.logger import LOGGER
                LOGGER.warning(f"create_token_api: subscription upsert failed for tg_id={telegram_user_id}: {sub_err}")

        return new_token
    except Exception as e:
        _logger.error("Internal error", exc_info=True)

        raise HTTPException(status_code=500, detail="Sunucu hatası")

async def update_token_limits_api(token: str, payload: dict):
    try:
        daily_limit = payload.get("daily_limit_gb")
        monthly_limit = payload.get("monthly_limit_gb")
        speed_limit = payload.get("speed_limit_mbps")
        portal_username = payload.get("portal_username")
        portal_password = payload.get("portal_password")
        validity_days = payload.get("validity_days")
        monthly_request_limit = payload.get("monthly_request_limit")
        telegram_user_id = payload.get("telegram_user_id")
        device_limit = payload.get("device_limit")
        ip_limit = payload.get("ip_limit")

        def parse_limit(val):
            try:
                v = float(val)
                return v if v > 0 else None
            except (ValueError, TypeError, AttributeError):
                return None

        def parse_int_limit(val):
            try:
                v = int(val)
                return v if v > 0 else None
            except (ValueError, TypeError, AttributeError):
                return None

        # validity_days → expires_at hesapla (mevcut expires_at + validity_days, yoksa bugün + validity_days)
        expires_at = None
        if validity_days is not None:
            try:
                days = int(validity_days)
                if days > 0:
                    from datetime import datetime, timedelta, timezone
                    existing_token = await db.get_api_token(token)
                    # Başlangıç: önce token'ın expires_at'ine bak
                    current_expires = existing_token.get("expires_at") if existing_token else None
                    if isinstance(current_expires, str):
                        try:
                            current_expires = datetime.fromisoformat(current_expires.replace("Z", "+00:00"))
                        except Exception:
                            current_expires = None
                    # Token'da expires_at yoksa, bağlı Telegram kullanıcısının subscription_expiry'sine bak
                    if current_expires is None and telegram_user_id:
                        try:
                            tg_user = await db.get_user(int(telegram_user_id))
                            if tg_user:
                                current_expires = tg_user.get("subscription_expiry")
                                if isinstance(current_expires, str):
                                    try:
                                        current_expires = datetime.fromisoformat(current_expires.replace("Z", "+00:00"))
                                    except Exception:
                                        current_expires = None
                        except Exception:
                            current_expires = None
                    if current_expires is not None:
                        # Naive ise UTC-aware yap
                        if hasattr(current_expires, 'tzinfo') and current_expires.tzinfo is None:
                            current_expires = current_expires.replace(tzinfo=timezone.utc)
                        # Eğer süre zaten dolmuşsa bugünden başlat
                        now_utc = datetime.now(timezone.utc)
                        base_date = current_expires if current_expires > now_utc else now_utc
                    else:
                        base_date = datetime.now(timezone.utc)
                    expires_at = base_date + timedelta(days=days)
                elif days == 0:
                    expires_at = None  # 0 = sınırsız, mevcut süreyi kaldır
            except (ValueError, TypeError):
                pass

        result = await db.update_api_token_limits(
            token,
            parse_limit(daily_limit),
            parse_limit(monthly_limit),
            parse_limit(speed_limit),
            portal_username=portal_username,
            portal_password=portal_password,
            expires_at=expires_at,
            clear_expiry=(validity_days is not None and int(validity_days) == 0),
            validity_days=int(validity_days) if validity_days is not None else None,
            telegram_user_id=int(telegram_user_id) if telegram_user_id else None,
            monthly_request_limit=int(monthly_request_limit) if monthly_request_limit is not None else None,
            device_limit=parse_int_limit(device_limit),
            ip_limit=parse_int_limit(ip_limit),
        )

        # Telegram ID varsa abonelik kaydını da güncelle/oluştur
        if telegram_user_id:
            try:
                tg_id = int(telegram_user_id)
                if expires_at is not None:
                    # expires_at zaten created_at + validity_days olarak hesaplandı, direkt kullan
                    await db.assign_subscription(tg_id, 0, force_expiry=expires_at)
                elif validity_days is not None and int(validity_days) == 0:
                    # 0 = sınırsız (100 yıl)
                    await db.assign_subscription(tg_id, 36500)
                # validity_days gönderilmediyse aboneliğe dokunma
            except Exception as sub_err:
                from Backend.logger import LOGGER
                LOGGER.warning(f"update_token_limits_api: subscription upsert failed for tg_id={telegram_user_id}: {sub_err}")

        if result:
            return {"message": "Limits updated successfully"}
        else:
            return {"message": "Limits updated successfully"}

    except Exception as e:
        _logger.error("Internal error", exc_info=True)

        raise HTTPException(status_code=500, detail="Sunucu hatası")

async def revoke_token_api(token: str, delete_subscription: bool = False, user_id: int = None):
    try:
        result = await db.revoke_api_token(token)
        if result:
            if delete_subscription and user_id:
                await db.manage_subscriber(user_id, "delete")
                await db.delete_user_reminders(user_id)
            return {"message": "Token revoked successfully"}
        else:
            raise HTTPException(status_code=404, detail="Token not found")
    except HTTPException:
        raise
    except Exception as e:
        _logger.error("Internal error", exc_info=True)

        raise HTTPException(status_code=500, detail="Sunucu hatası")


async def regenerate_token_api(token: str):
    """Aynı üyeye ait tüm limit/kullanım verilerini koruyarak token string'ini
    yeniler; eski token anında geçersiz olur."""
    try:
        result = await db.regenerate_api_token(token)
        if result:
            return result
        else:
            raise HTTPException(status_code=404, detail="Token not found")
    except HTTPException:
        raise
    except Exception as e:
        _logger.error("Internal error", exc_info=True)

        raise HTTPException(status_code=500, detail="Sunucu hatası")


# --- Speed Test API ---

async def speed_test_api(
    quality_id: str = Query(..., description="Encoded quality ID from DB"),
    tmdb_id: int = Query(...),
    db_index: int = Query(...),
    media_type: str = Query(..., regex="^(movie|tv)$"),
):
    """
    Decode quality_id using the same decode_string logic as the stream handler,
    then run a parallel download speed test across all connected bot clients.
    """
    from Backend.helper.encrypt import decode_string

    try:
        decoded = await decode_string(quality_id)
        msg_id  = decoded.get("msg_id")
        raw_cid = decoded.get("chat_id")

        # Yerel dosya (zipmodu URL modu) — Telegram'da değil, stream testi yapılamaz
        if decoded.get("local_path"):
            raise HTTPException(
                status_code=422,
                detail="Bu dosya yerel diskten yayınlanmaktadır (zipmodu). Telegram tabanlı hız testi yapılamaz."
            )

        if not msg_id or not raw_cid:
            raise HTTPException(
                status_code=422,
                detail=f"Decoded quality data is missing msg_id or chat_id. Decoded: {decoded}"
            )

        # Stream handler adds -100 prefix for channel IDs
        chat_id = int(f"-100{raw_cid}")

        results = await run_speed_test(int(chat_id), int(msg_id))
        return {"results": results, "total_clients_tested": len(results)}

    except HTTPException:
        raise
    except Exception as e:
        _logger.error("Internal error", exc_info=True)

        raise HTTPException(status_code=500, detail="Sunucu hatası")


# --- Speed Test SSE Streaming API ---

async def speed_test_stream_api(
    quality_id: str,
    tmdb_id: int,
    db_index: int,
    media_type: str,
):
    """
    SSE version of the speed test. Streams each per-client result as a
    'data:' event the moment that client finishes, so the UI can update live.
    """
    from Backend.helper.encrypt import decode_string

    async def event_generator():
        # Decode quality_id → chat_id + message_id
        try:
            decoded = await decode_string(quality_id)
            msg_id  = decoded.get("msg_id")
            raw_cid = decoded.get("chat_id")

            # Yerel dosya (zipmodu URL modu) — hız testi yapılamaz
            if decoded.get("local_path"):
                payload = json.dumps({
                    "type": "error",
                    "message": "Bu dosya yerel diskten yayınlanmaktadır (zipmodu). Telegram tabanlı hız testi yapılamaz."
                })
                yield f"data: {payload}\n\n"
                return

            if not msg_id or not raw_cid:
                payload = json.dumps({"type": "error", "message": f"Cannot decode quality_id. Got: {decoded}"})
                yield f"data: {payload}\n\n"
                return
            chat_id = int(f"-100{raw_cid}")
        except Exception as exc:
            payload = json.dumps({"type": "error", "message": str(exc)})
            yield f"data: {payload}\n\n"
            return

        total = len(multi_clients)
        if total == 0:
            payload = json.dumps({"type": "error", "message": "No bot clients connected"})
            yield f"data: {payload}\n\n"
            return
            
        # Try to resolve the FileId to get the target DC
        target_dc = "?"
        try:
            from Backend.helper.custom_dl import ByteStreamer
            primary_client = multi_clients.get(0) or next(iter(multi_clients.values()))
            streamer = ByteStreamer(primary_client)
            file_id = await streamer.get_file_properties(chat_id, int(msg_id))
            target_dc = file_id.dc_id
        except Exception:
            pass

        # Send initial "start" event so the frontend can set up the table
        yield f"data: {json.dumps({'type': 'start', 'total': total, 'target_dc': target_dc})}\n\n"

        # Run all clients in parallel; feed results into a queue as they finish
        queue: asyncio.Queue = asyncio.Queue()

        async def run_one(client, idx):
            async def on_progress(prog_data):
                await queue.put({"type": "progress", "data": prog_data})
                
            result = await _speed_test_single_client(
                client, idx, chat_id, int(msg_id), progress_callback=on_progress
            )
            await queue.put({"type": "result", "data": result})

        tasks = [
            asyncio.create_task(run_one(client, idx))
            for idx, client in multi_clients.items()
        ]

        completed = 0
        while completed < total:
            msg = await queue.get()
            
            if msg["type"] == "progress":
                payload = json.dumps(msg)
                yield f"data: {payload}\n\n"
            
            elif msg["type"] == "result":
                completed += 1
                payload = json.dumps({
                    "type": "result",
                    "data": msg["data"],
                    "completed": completed,
                    "total": total,
                })
                yield f"data: {payload}\n\n"

        # Wait for any remaining tasks (should already be done)
        await asyncio.gather(*tasks, return_exceptions=True)

        # Final done event
        yield f"data: {json.dumps({'type': 'done', 'total': total})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # prevent nginx from buffering SSE
        },
    )

# ---------------------------------------------------------------------------
# Admin API Routes
# ---------------------------------------------------------------------------

async def get_admin_stats_api() -> dict:
    from Backend.pyrofork.bot import work_loads, multi_clients, client_failures, client_avg_mbps
    from Backend.fastapi.routes.stream_routes import _streamer_by_client
    
    # Sum cache entries across all active ByteStreamer instances
    cache_size = sum(len(s._file_id_cache) for s in _streamer_by_client.values())
    
    # Calculate bot workloads and health
    bot_stats = []
    for client_index in multi_clients:
        load = work_loads.get(client_index, 0)
        failures = client_failures.get(client_index, 0)
        mbps = client_avg_mbps.get(client_index, 0.0)
        
        status = "healthy"
        if failures > 5:
            status = "degraded"
        if failures > 15:
            status = "failing"
            
        bot_stats.append({
            "client_index": client_index,
            "display_name": f"Bot {client_index + 1}",
            "current_load": load,
            "failures": failures,
            "avg_mbps": round(mbps, 2),
            "status": status
        })
        
    return {
        "cache_size": cache_size,
        "total_bots": len(multi_clients),
        "bot_workloads": bot_stats
    }

async def clear_cache_api() -> dict:
    from Backend.fastapi.routes.stream_routes import _streamer_by_client
    from Backend.logger import LOGGER
    
    # Clear cache across all active ByteStreamer instances
    total_cleared = sum(len(s._file_id_cache) for s in _streamer_by_client.values())
    for streamer in _streamer_by_client.values():
        streamer._file_id_cache.clear()
    LOGGER.info(f"Admin cleared the FileId cache ({total_cleared} items purged across {len(_streamer_by_client)} clients).")
    
    return {"status": "success", "message": f"{total_cleared} cached items cleared."}

async def get_dead_links_api() -> dict:
    from Backend import db
    try:
        dead_links = await db.get_all_dead_links()
        return {"status": "success", "data": dead_links}
    except Exception as e:
        _logger.error("Internal error", exc_info=True)

        return {"status": "error", "message": "Sunucu hatası"}

async def get_stream_analytics_api() -> dict:
    from Backend import db
    try:
        data = await db.get_stream_analytics(limit=20)
        return {"status": "success", "data": data}
    except Exception as e:
        from Backend.logger import LOGGER
        LOGGER.error(f"Stream analytics API error: {e}")
        _logger.error("Internal error", exc_info=True)

        return {"status": "error", "message": "Sunucu hatası"}

async def clear_analytics_api() -> dict:
    from Backend import db
    from Backend.logger import LOGGER
    try:
        col = db.dbs["tracking"]["stream_analytics"]
        result = await col.delete_many({})
        LOGGER.info(f"Admin cleared stream analytics ({result.deleted_count} records deleted).")
        return {"status": "success", "message": f"{result.deleted_count} analiz kaydı temizlendi."}
    except Exception as e:
        LOGGER.error(f"Clear analytics error: {e}")
        _logger.error("Internal error", exc_info=True)

        return {"status": "error", "message": "Sunucu hatası"}

# ---------------------------------------------------------------------------
# Admin Subscription Management API Routes
# ---------------------------------------------------------------------------

async def get_subscription_plans_api() -> dict:
    from Backend import db
    try:
        plans = await db.get_subscription_plans()
        return {"status": "success", "data": plans}
    except Exception as e:
        _logger.error("Internal error", exc_info=True)

        return {"status": "error", "message": "Sunucu hatası"}

async def add_subscription_plan_api(payload: dict) -> dict:
    from Backend import db
    try:
        days = int(payload.get("days", 0))
        price = float(payload.get("price", 0.0))
        label = str(payload.get("label", "")).strip()
        currency = str(payload.get("currency", "USD")).strip().upper()
        is_unlimited = bool(payload.get("is_unlimited", False))
        daily_limit_gb = float(payload.get("daily_limit_gb") or 0)
        monthly_limit_gb = float(payload.get("monthly_limit_gb") or 0)
        speed_limit_mbps = float(payload.get("speed_limit_mbps") or 0)
        monthly_request_limit = int(payload.get("monthly_request_limit") or 0)
        if not is_unlimited and days <= 0:
            raise HTTPException(status_code=400, detail="Invalid plan parameters")
        if price < 0:
            raise HTTPException(status_code=400, detail="Invalid plan parameters")
            
        plan_id = await db.add_subscription_plan(days, price, label, currency, is_unlimited, daily_limit_gb, monthly_limit_gb, speed_limit_mbps, monthly_request_limit=monthly_request_limit)
        if plan_id:
            return {"status": "success", "message": "Plan added successfully", "plan_id": plan_id}
        else:
            raise HTTPException(status_code=500, detail="Failed to add plan")
    except HTTPException:
        raise
    except Exception as e:
        _logger.error("Internal error", exc_info=True)

        raise HTTPException(status_code=500, detail="Sunucu hatası")

async def update_subscription_plan_api(plan_id: str, payload: dict) -> dict:
    from Backend import db
    try:
        days = int(payload.get("days", 0))
        price = float(payload.get("price", 0.0))
        label = str(payload.get("label", "")).strip()
        currency = str(payload.get("currency", "USD")).strip().upper()
        is_unlimited = bool(payload.get("is_unlimited", False))
        daily_limit_gb = float(payload.get("daily_limit_gb") or 0)
        monthly_limit_gb = float(payload.get("monthly_limit_gb") or 0)
        speed_limit_mbps = float(payload.get("speed_limit_mbps") or 0)
        monthly_request_limit = int(payload.get("monthly_request_limit") or 0)
        if not is_unlimited and days <= 0:
             raise HTTPException(status_code=400, detail="Invalid plan parameters")
        if price < 0:
             raise HTTPException(status_code=400, detail="Invalid plan parameters")
             
        success = await db.update_subscription_plan(plan_id, days, price, label, currency, is_unlimited, daily_limit_gb, monthly_limit_gb, speed_limit_mbps, monthly_request_limit=monthly_request_limit)
        if success:
             return {"status": "success", "message": "Plan updated successfully"}
        else:
             raise HTTPException(status_code=404, detail="Plan not found or update failed")
    except HTTPException:
         raise
    except Exception as e:
         _logger.error("Internal error", exc_info=True)

         raise HTTPException(status_code=500, detail="Sunucu hatası")

async def delete_subscription_plan_api(plan_id: str) -> dict:
    from Backend import db
    try:
        success = await db.delete_subscription_plan(plan_id)
        if success:
            return {"status": "success", "message": "Plan deleted successfully"}
        else:
            raise HTTPException(status_code=404, detail="Plan not found")
    except HTTPException:
        raise
    except Exception as e:
        _logger.error("Internal error", exc_info=True)

        raise HTTPException(status_code=500, detail="Sunucu hatası")

async def get_all_subscribers_api() -> dict:
    from Backend import db
    try:
        users = await db.get_all_subscribers()
        return {"status": "success", "data": users}
    except Exception as e:
        _logger.error("Internal error", exc_info=True)

        return {"status": "error", "message": "Sunucu hatası"}

async def manage_subscriber_api(user_id: int, payload: dict) -> dict:
    from Backend import db
    try:
        action = payload.get("action")
        days = int(payload.get("days", 0))
        
        if action not in ["extend", "reduce", "delete"]:
            raise HTTPException(status_code=400, detail="Invalid action")
            
        success = await db.manage_subscriber(user_id, action, days)
        if success:
            return {"status": "success", "message": "User subscription updated successfully"}
        else:
            raise HTTPException(status_code=404, detail="User not found or update failed")
    except HTTPException:
        raise
    except Exception as e:
        _logger.error("Internal error", exc_info=True)

        raise HTTPException(status_code=500, detail="Sunucu hatası")


async def get_pending_subscription_requests_api() -> dict:
    """Web panelinde 'Bekleyen Abonelik Talepleri' listesi için kullanılır."""
    from Backend import db
    try:
        pending = await db.get_pending_subscription_payments()
        return {"status": "success", "requests": pending}
    except Exception as e:
        _logger.error("Internal error", exc_info=True)
        return {"status": "error", "message": "Sunucu hatası"}


def _get_websitesi_enabled_api() -> bool:
    """config.env'den WEBSITESI değerini runtime'da okur (bot restart gerekmez)."""
    import re as _re
    try:
        text = pathlib.Path("config.env").read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
        m = _re.search(r'^WEBSITESI\s*=\s*["\']?(.*?)["\']?\s*(?:#.*)?$', text, _re.MULTILINE)
        if m:
            return m.group(1).strip().lower() == "true"
    except Exception:
        pass
    return True  # Bulunamazsa varsayılan: açık


async def admin_review_subscription_request_api(user_id: int, payload: dict) -> dict:
    """
    Web panelinden bekleyen abonelik talebini onayla / reddet / banla.

    Botla tam senkron çalışır: Telegram bot'undaki (Backend/pyrofork/plugins/subscription.py
    admin_review) ile AYNI DB fonksiyonlarını kullanır ve kullanıcıya sonucu Telegram üzerinden
    bildirir. Ayrıca yöneticilere bot tarafından gönderilmiş olan bekleyen onay mesajını
    (✅ Onayla / ❌ Reddet / 🚫 Banla butonlarıyla) günceller: butonları kaldırır ve talebin
    web panelinden hangi kararla sonuçlandığını ekler — böylece aynı talep botta tekrar
    işleme alınmaya çalışılamaz ve panel/bot arasında tutarsızlık oluşmaz.
    """
    from Backend import db
    from Backend.config import Telegram
    from pyrogram import enums as _enums

    action = (payload or {}).get("action")
    if action not in ("approve", "reject", "ban"):
        raise HTTPException(status_code=400, detail="Geçersiz aksiyon")

    user = await db.get_user(user_id)

    # Ban işlemi için pending_payment zorunlu değil (bot tarafındaki davranışla aynı)
    if action != "ban" and (not user or "pending_payment" not in user):
        raise HTTPException(status_code=404, detail="Bu talep zaten işleme alınmış.")

    admin_messages = ((user or {}).get("pending_payment") or {}).get("admin_messages") or []
    user_name = (user or {}).get("first_name") or (user or {}).get("username") or str(user_id)

    async def _update_admin_messages(label: str):
        """
        Botun yöneticilere gönderdiği bekleyen onay mesajını günceller: onayla/reddet/banla
        butonlarını kaldırır ve talebin web panelinden hangi kararla sonuçlandığını ekler.
        Bu sayede talep botta hâlâ 'beklemede' görünmeye devam etmez.
        """
        if not admin_messages:
            return
        status_section = (
            f"\n\n{'─' * 30}\n"
            f"<b>{label}</b> — 🌐 Web panelinden\n"
            f"<b>👤 Kullanıcı:</b> {user_name} (ID: <code>{user_id}</code>)"
        )
        for am in admin_messages:
            try:
                existing_msg = await StreamBot.get_messages(am["chat_id"], am["message_id"])
                original_text = existing_msg.text or existing_msg.caption or ""
            except Exception:
                original_text = ""
            try:
                await StreamBot.edit_message_text(
                    chat_id=am["chat_id"],
                    message_id=am["message_id"],
                    text=f"{original_text}{status_section}" if original_text else status_section,
                    parse_mode=_enums.ParseMode.HTML,
                    disable_web_page_preview=True,
                    reply_markup=None,
                )
            except Exception as e:
                _logger.warning(
                    "Panel işlemi sonrası admin mesajı güncellenemedi (%s/%s): %s",
                    am.get("chat_id"), am.get("message_id"), e
                )

    if action == "approve":
        user_data = await db.approve_payment(user_id)
        if not user_data:
            raise HTTPException(status_code=404, detail="Onaylanamadı — bekleyen talep bulunamadı.")

        try:
            await db.reset_reminder_sent(user_id)
        except Exception:
            pass

        user_name = user_data.get("first_name") or user_data.get("username") or str(user_id)
        try:
            token_doc = await db.add_api_token(
                name=user_name,
                user_id=user_id,
                daily_limit_gb=user_data.get("_plan_daily_gb") or None,
                monthly_limit_gb=user_data.get("_plan_monthly_gb") or None,
                speed_limit_mbps=user_data.get("_plan_speed_mbps") or None,
            )
            token_str = token_doc.get("token")
        except Exception:
            token_str = None

        base_url = Telegram.BASE_URL
        expiry = user_data.get("subscription_expiry")
        expiry_str = expiry.strftime("%d.%m.%Y") if expiry else "—"

        try:
            if _get_websitesi_enabled_api():
                otp = await db.create_member_otp(user_id, user_name)
                portal_url = f"{base_url}/uye/giris"
                otp_text = (
                    f"\n\n🌐 <b>Dizi ve filmleri indirmek için:</b>\n"
                    f"🔗 {portal_url}\n"
                    f"👤 <b>Kullanıcı Adı:</b> <code>{otp['username']}</code>\n"
                    f"🔑 <b>Şifre:</b> <code>{otp['password']}</code>\n"
                    f"<i>⚠️ Bu bilgiler her /start'ta yenilenir.</i>"
                )
            else:
                otp_text = (
                    f"\n\n🔧 <b>{Telegram.ISIM} Websitesi</b> şu an bakım çalışmasındadır.\n"
                    f"<i>Hizmet kısa süre içinde tekrar aktif olacaktır.</i>"
                )
        except Exception:
            otp_text = ""

        if token_str:
            tr_url = f"{base_url}/stremio/{token_str}/tr/manifest.json"
            de_url = f"{base_url}/stremio/{token_str}/de/manifest.json"
            en_url = f"{base_url}/stremio/{token_str}/en/manifest.json"
            success_text = (
                f"✅ <b>Aboneliğiniz aktif durumdadır.</b>\n"
                f"📅 <b>Son kullanma tarihi:</b> {expiry_str}\n\n"
                f"🔗 <b>Eklenti linkiniz:</b>\n\n"
                f"🇹🇷 <b>Türkçe:</b>\n<code>{tr_url}</code>\n\n"
                f"🇩🇪 <b>Deutsch:</b>\n<code>{de_url}</code>\n\n"
                f"🇬🇧 <b>English:</b>\n<code>{en_url}</code>\n\n"
                f"Dizi ve filmleri izlemek için yukarıdaki linki kopyalayıp Nuvio eklentilerine yapıştırın."
                f"{otp_text}"
            )
        else:
            success_text = (
                f"✅ <b>Aboneliğiniz aktif durumdadır.</b>\n"
                f"📅 <b>Son kullanma tarihi:</b> {expiry_str}\n\n"
                f"⚠️ Eklenti linkiniz oluşturulurken sorun oluştu. Lütfen yönetici ile iletişime geçin."
                f"{otp_text}"
            )

        try:
            await StreamBot.send_message(user_id, success_text, parse_mode=_enums.ParseMode.HTML)
        except Exception as e:
            _logger.warning("Kullanıcıya onay bildirimi gönderilemedi (%s): %s", user_id, e)

        await _update_admin_messages("✅ Onaylandı")
        return {"status": "success", "message": f"{user_name} için talep onaylandı ve kullanıcıya bildirildi."}

    elif action == "reject":
        success = await db.reject_payment(user_id)
        if not success:
            raise HTTPException(status_code=404, detail="Reddedilemedi — bekleyen talep bulunamadı.")

        try:
            await StreamBot.send_message(
                user_id,
                "❌ <b>Talebiniz Reddedildi</b>\n\nAbonelik talebiniz yönetici tarafından reddedildi. "
                "Daha fazla bilgi için yönetici ile iletişime geçin.",
                parse_mode=_enums.ParseMode.HTML
            )
        except Exception as e:
            _logger.warning("Kullanıcıya red bildirimi gönderilemedi (%s): %s", user_id, e)

        await _update_admin_messages("❌ Reddedildi")
        return {"status": "success", "message": "Talep reddedildi ve kullanıcıya bildirildi."}

    else:  # ban
        await db.ban_user(user_id)
        try:
            await StreamBot.send_message(
                user_id,
                "🚫 <b>Hesabınız Engellendi</b>\n\nHesabınız yönetici tarafından engellenmiştir. "
                "Bu botu artık kullanamazsınız.",
                parse_mode=_enums.ParseMode.HTML
            )
        except Exception:
            pass

        await _update_admin_messages("🚫 Banlandı")
        return {"status": "success", "message": "Kullanıcı banlandı."}


# --- Access Management API ---

async def get_all_tokens_api() -> dict:
    from Backend import db
    from Backend.config import Telegram
    from datetime import datetime
    try:
        tokens = await db.get_all_api_tokens()
        now = datetime.utcnow()
        result = []

        # Pre-load all subscribers into a dict keyed by user_id for O(1) lookup
        subscriber_map = {}       # user_id (str) -> user doc
        if Telegram.SUBSCRIPTION:
            try:
                for u in await db.get_all_subscribers():
                    uid = str(u.get("_id"))
                    subscriber_map[uid] = u
            except Exception:
                pass

        def display_name(user, user_id, token_name=None):
            """Return a non-empty display name for a user."""
            if user:
                n = user.get("first_name") or user.get("username")
                if n:
                    return n
            # Fall back to the name stored on the token itself (set at creation time)
            if token_name:
                return token_name
            return f"User {user_id}" if user_id else "Telegram User"

        def build_entry(user_id, user, token_doc):
            """Build a unified access entry from optional user + token records."""
            expiry = None
            sub_status = None
            user_found = bool(user)

            if user:
                sub_status = user.get("subscription_status")
                expiry = user.get("subscription_expiry")

            # Token-level expiry as fallback
            if token_doc:
                t_expiry = token_doc.get("subscription_expiry") or token_doc.get("expires_at")
                if t_expiry and not expiry:
                    expiry = t_expiry

            # Determine status
            if Telegram.SUBSCRIPTION:
                if not user_found:
                    is_expired = True
                elif sub_status != "active":
                    is_expired = True
                elif not expiry:
                    is_expired = True
                else:
                    is_expired = expiry < now
            else:
                is_expired = bool(expiry and expiry < now)

            token_str = token_doc.get("token") if token_doc else None
            created = token_doc.get("created_at") if token_doc else (user.get("created_at") if user else None)

            return {
                "token": token_str,
                "user_id": user_id,
                "user_name": display_name(user, user_id, token_doc.get("name") if token_doc else None),
                "user_found": user_found,
                "has_token": bool(token_str),
                "created_at": created.isoformat() if created else None,
                "expires_at": expiry.isoformat() if expiry else None,
                "is_expired": is_expired,
                "sub_status": sub_status,
                "addon_url": (
                    f"{Telegram.BASE_URL}/stremio/{token_str}/manifest.json"
                    if token_str else None
                ),
            }

        # Track user_ids that are already represented via a token row
        seen_user_ids = set()

        # --- 1. Process all existing tokens ---
        for t in tokens:
            token_user_id = t.get("user_id")

            # Try to resolve user from subscriber_map using token's user_id
            user = None
            if token_user_id:
                uid_str = str(token_user_id)
                user = subscriber_map.get(uid_str)
                if not user:
                    # Fallback: query DB if not in subscriber_map (e.g. non-active subscribers)
                    try:
                        user = await db.get_user(int(token_user_id))
                    except Exception:
                        pass
                seen_user_ids.add(uid_str)

            result.append(build_entry(token_user_id, user, t))

        # --- 2. Add subscribers who have NO token ---
        for uid_str, u in subscriber_map.items():
            if uid_str in seen_user_ids:
                continue  # already covered by a token row
            result.append(build_entry(u.get("_id"), u, None))

        # Sort: active-with-token first, then active-no-token, expired last
        result.sort(key=lambda x: (x["is_expired"], not x["has_token"]))
        return {"tokens": result}
    except Exception as e:
        _logger.error("Internal error", exc_info=True)

        raise HTTPException(status_code=500, detail="Sunucu hatası")


async def revoke_token_api(token: str, delete_subscription: bool = False, user_id: int = None) -> dict:
    from Backend import db
    try:
        success = await db.revoke_api_token(token)
        if not success:
            raise HTTPException(status_code=404, detail="Token not found.")
        # Token silindikten sonra stream_analytics'te kalan "yetim" kayıtları da temizle;
        # aksi halde dashboard'daki Uyarılar kartında artık var olmayan bu üye için
        # sahte bir "GB Tutarsızlığı" uyarısı görünmeye devam eder.
        try:
            await db.purge_stream_analytics_for_token(token)
        except Exception as purge_err:
            _logger.warning(f"revoke_token_api: stream_analytics purge failed: {purge_err}")
        if delete_subscription and user_id:
            await db.manage_subscriber(user_id, "delete")
            await db.delete_user_reminders(user_id)
            await db.delete_user_content_requests(user_id)
        return {"status": "success", "message": "Token (ve varsa abonelik) silindi."}
    except HTTPException:
        raise
    except Exception as e:
        _logger.error("Internal error", exc_info=True)

        raise HTTPException(status_code=500, detail="Sunucu hatası")


async def assign_plan_api(user_id: int, days: int) -> dict:
    """Assign (or extend) a subscription for any user by user_id, even if not in DB."""
    from Backend import db
    try:
        if days < 1:
            raise HTTPException(status_code=400, detail="Days must be at least 1.")
        result = await db.assign_subscription(user_id, days)
        return {"status": "success", "data": result}
    except HTTPException:
        raise
    except Exception as e:
        _logger.error("Internal error", exc_info=True)

        raise HTTPException(status_code=500, detail="Sunucu hatası")


async def link_token_user_api(token: str, user_id: int) -> dict:
    """Link an orphan token (no user_id) to a Telegram user_id."""
    from Backend import db
    try:
        success = await db.link_token_user(token, user_id)
        if success:
            return {"status": "success", "message": f"Token linked to user {user_id}."}
        raise HTTPException(status_code=404, detail="Token not found or already linked.")
    except HTTPException:
        raise
    except Exception as e:
        _logger.error("Internal error", exc_info=True)

        raise HTTPException(status_code=500, detail="Sunucu hatası")


async def rename_movie_quality_api(request: Request, tmdb_id: int, db_index: int, quality_id: str):
    """Film kalitesinin 'name' alanını günceller."""
    from Backend import db
    try:
        body = await request.json()
        new_name = body.get("name", "").strip()
        if not new_name:
            raise HTTPException(status_code=400, detail="'name' alanı boş olamaz.")
        result = await db.rename_movie_quality(tmdb_id, db_index, quality_id, new_name)
        if result:
            return {"message": "İsim başarıyla güncellendi"}
        raise HTTPException(status_code=404, detail="Kalite bulunamadı veya değişiklik yapılamadı")
    except HTTPException:
        raise
    except Exception as e:
        _logger.error("Internal error", exc_info=True)

        raise HTTPException(status_code=500, detail="Sunucu hatası")


async def rename_tv_quality_api(request: Request, tmdb_id: int, db_index: int, season: int, episode: int, quality_id: str):
    """Dizi bölümü kalitesinin 'name' alanını günceller."""
    from Backend import db
    try:
        body = await request.json()
        new_name = body.get("name", "").strip()
        if not new_name:
            raise HTTPException(status_code=400, detail="'name' alanı boş olamaz.")
        result = await db.rename_tv_quality(tmdb_id, db_index, season, episode, quality_id, new_name)
        if result:
            return {"message": "İsim başarıyla güncellendi"}
        raise HTTPException(status_code=404, detail="Kalite bulunamadı veya değişiklik yapılamadı")
    except HTTPException:
        raise
    except Exception as e:
        _logger.error("Internal error", exc_info=True)

        raise HTTPException(status_code=500, detail="Sunucu hatası")


async def requery_media_api(request: Request, tmdb_id: int, db_index: int, media_type: str):
    """
    Mevcut kaydın dosya adlarından (telegram[].name) PTN ile metadata çıkarır,
    TMDB'den güncel bilgileri getirir ve önizleme olarak döndürür.
    Onaylandığında /api/media/update ile kaydedilebilir.
    """
    import re
    import PTN
    from Backend.helper.metadata import (
        safe_tmdb_search,
        _tmdb_movie_details,
        _tmdb_tv_details,
        format_tmdb_image,
        get_tmdb_logo,
        _fetch_tmdb_images,
    )
    try:
        from Backend.helper.metadata import tur_genre_normalize, de_genre_normalize
    except ImportError:
        def tur_genre_normalize(g): return g
        def de_genre_normalize(g): return g

    try:
        doc = await db.get_document(media_type, tmdb_id, db_index)
        if not doc:
            raise HTTPException(status_code=404, detail="Kayıt bulunamadı")

        # Dosya adlarını topla
        filenames: list[str] = []
        if media_type == "movie":
            for q in doc.get("telegram", []):
                n = q.get("name", "")
                if n:
                    filenames.append(n)
        else:  # tv
            for season in doc.get("seasons", []):
                for ep in season.get("episodes", []):
                    for q in ep.get("telegram", []):
                        n = q.get("name", "")
                        if n:
                            filenames.append(n)

        if not filenames:
            raise HTTPException(status_code=400, detail="Kayıtta dosya adı bulunamadı")

        # En iyi dosya adını seç (en uzun / en bilgi dolu)
        best_filename = max(filenames, key=lambda f: len(PTN.parse(f)))
        best_filename_clean = re.sub(r'https?://\S+', '', best_filename).strip()
        best_filename_clean = re.sub(r'\bm(1080p|720p|2160p|480p)\b', r'\1', best_filename_clean, flags=re.IGNORECASE)

        parsed = PTN.parse(best_filename_clean)
        title = parsed.get("title") or doc.get("title", "")
        year  = parsed.get("year")
        season_num  = parsed.get("season")
        episode_num = parsed.get("episode")

        if not title:
            raise HTTPException(status_code=400, detail="Dosya adından başlık çıkarılamadı")

        # TMDB arama
        is_tv = media_type == "tv" or bool(season_num and episode_num)
        tmdb_type = "tv" if is_tv else "movie"

        result = await safe_tmdb_search(title, tmdb_type, year)
        if not result:
            raise HTTPException(status_code=404, detail=f"TMDB'de '{title}' bulunamadı")

        new_tmdb_id = result.id

        # TMDB detayları
        if is_tv:
            details = await _tmdb_tv_details(new_tmdb_id)
        else:
            details = await _tmdb_movie_details(new_tmdb_id)

        if not details:
            raise HTTPException(status_code=404, detail="TMDB detayları alınamadı")

        # Görsel verileri
        images = await _fetch_tmdb_images(tmdb_type, new_tmdb_id)

        def _img(path, size="w500"):
            return format_tmdb_image(path, size) if path else ""

        if is_tv:
            genres_raw = [g.name for g in (getattr(details, "genres", None) or [])]
            preview = {
                "tmdb_id":        new_tmdb_id,
                "title":          details.original_name or details.name or title,
                "title_tr":       details.name or title,
                "title_de":       getattr(details, "name_de", "") or details.original_name or title,
                "description":    getattr(details, "overview", "") or "",
                "description_tr": getattr(details, "overview_tr", "") or getattr(details, "overview", "") or "",
                "description_de": getattr(details, "overview_de", "") or "",
                "release_year":   getattr(getattr(details, "first_air_date", None), "year", None),
                "rating":         getattr(details, "vote_average", None),
                "poster":         _img(getattr(details, "poster_path", None)),
                "backdrop":       _img(getattr(details, "backdrop_path", None), "original"),
                "logo":           get_tmdb_logo(getattr(details, "images", None)),
                "poster_tr":      getattr(details, "poster_tr", "") or "",
                "backdrop_tr":    getattr(details, "backdrop_tr", "") or "",
                "logo_tr":        getattr(details, "logo_tr", "") or "",
                "poster_de":      getattr(details, "poster_de", "") or "",
                "backdrop_de":    getattr(details, "backdrop_de", "") or "",
                "logo_de":        getattr(details, "logo_de", "") or "",
                "genres":         genres_raw,
                "genres_tr":      tur_genre_normalize(genres_raw),
                "genres_de":      de_genre_normalize(getattr(details, "genres_de", []) or []) or de_genre_normalize(genres_raw),
                "original_language": getattr(details, "original_language", None),
                "runtime":        str(getattr(details, "episode_run_time", [None])[0] or "") if getattr(details, "episode_run_time", None) else "",
                "total_seasons":  getattr(details, "number_of_seasons", None),
                "total_episodes": getattr(details, "number_of_episodes", None),
                "certification_tr": getattr(details, "certification_tr", None),
                "certification_de": getattr(details, "certification_de", None),
                "certification_us": getattr(details, "certification_us", None),
                "_parsed_from":   best_filename_clean,
            }
        else:
            genres_raw = [g.name for g in (getattr(details, "genres", None) or [])]
            runtime_raw = getattr(details, "runtime", None)
            preview = {
                "tmdb_id":        new_tmdb_id,
                "title":          details.original_title or getattr(details, "title", None) or title,
                "title_tr":       getattr(details, "title", None) or title,
                "title_de":       getattr(details, "title_de", "") or details.original_title or title,
                "description":    getattr(details, "overview", "") or "",
                "description_tr": getattr(details, "overview_tr", "") or getattr(details, "overview", "") or "",
                "description_de": getattr(details, "overview_de", "") or "",
                "release_year":   getattr(getattr(details, "release_date", None), "year", None),
                "rating":         getattr(details, "vote_average", None),
                "poster":         _img(getattr(details, "poster_path", None)),
                "backdrop":       _img(getattr(details, "backdrop_path", None), "original"),
                "logo":           get_tmdb_logo(getattr(details, "images", None)),
                "poster_tr":      getattr(details, "poster_tr", "") or "",
                "backdrop_tr":    getattr(details, "backdrop_tr", "") or "",
                "logo_tr":        getattr(details, "logo_tr", "") or "",
                "poster_de":      getattr(details, "poster_de", "") or "",
                "backdrop_de":    getattr(details, "backdrop_de", "") or "",
                "logo_de":        getattr(details, "logo_de", "") or "",
                "genres":         genres_raw,
                "genres_tr":      tur_genre_normalize(genres_raw),
                "genres_de":      de_genre_normalize(getattr(details, "genres_de", []) or []) or de_genre_normalize(genres_raw),
                "original_language": getattr(details, "original_language", None),
                "runtime":        str(runtime_raw) if runtime_raw else "",
                "collection_id":  getattr(getattr(details, "belongs_to_collection", None), "id", None),
                "certification_tr": getattr(details, "certification_tr", None),
                "certification_de": getattr(details, "certification_de", None),
                "certification_us": getattr(details, "certification_us", None),
                "_parsed_from":   best_filename_clean,
            }

        return {"preview": preview}

    except HTTPException:
        raise
    except Exception as e:
        _logger.error("Yeniden sorgulama hatası", exc_info=True)
        raise HTTPException(status_code=500, detail="Sunucu hatası")


# --- API Routes: Ayarlar (Settings) ---

async def get_settings_api():
    """Panelde gösterilecek güncel ayarları döner (hassas alanlar maskelenmez,
    çünkü bu uygulamada statik admin şifresi yok — kimlik doğrulama OTP tabanlı)."""
    try:
        from Backend.helper.settings_manager import SettingsManager, get_env_multi_tokens
        data = SettingsManager.current().to_dict()
        try:
            data["database_list"] = db.get_database_list()
        except Exception:
            data["database_list"] = []

        #----- config.env üzerinden tanımlanan MULTI_TOKEN_x değişkenleri:
        #----- bunlar ayarlar sayfasından silinemez/eklenemez, bu yüzden panelde
        #----- salt okunur (maskelenmiş) olarak ayrı gösterilir.
        try:
            data["env_multi_tokens"] = get_env_multi_tokens()
        except Exception:
            data["env_multi_tokens"] = []

        return {"success": True, "settings": data}
    except Exception as e:
        _logger.error("get_settings_api hatası", exc_info=True)
        raise HTTPException(status_code=500, detail="Ayarlar okunamadı")


async def update_settings_api(payload: dict):
    """Ayarlar sayfasından gelen değişiklikleri kaydeder ve ilgili
    bileşenleri (çoklu token, veritabanları, abonelik görevi vb.) yeniden başlatır."""
    try:
        from Backend.helper.settings_manager import SettingsManager
        results = await SettingsManager.update(db, payload or {})
        return {
            "success": True,
            "message": "Ayarlar kaydedildi.",
            "details": results,
            "settings": SettingsManager.current().to_dict(),
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        _logger.error("update_settings_api hatası", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Ayarlar kaydedilemedi: {e}")


async def export_settings_backup_api():
    """Ayarları indirilebilir bir JSON yedeği olarak döner."""
    try:
        from Backend.helper.settings_manager import SettingsManager
        data = SettingsManager.current().to_dict()
        data.pop("_id", None)
        payload = {
            "app": "Telegram-Stremio",
            "version": __version__,
            "exported_at": time(),
            "settings": data,
        }
        return JSONResponse(
            content=payload,
            headers={"Content-Disposition": "attachment; filename=ayarlar_yedek.json"},
        )
    except Exception as e:
        _logger.error("export_settings_backup_api hatası", exc_info=True)
        raise HTTPException(status_code=500, detail="Yedek oluşturulamadı")


async def import_settings_backup_api(payload: dict):
    """Daha önce dışa aktarılmış bir JSON yedeğini geri yükler."""
    try:
        from Backend.helper.settings_manager import SettingsManager
        incoming = payload.get("settings") if isinstance(payload, dict) and "settings" in payload else payload
        if not isinstance(incoming, dict):
            raise HTTPException(status_code=400, detail="Geçersiz yedek dosyası")
        results = await SettingsManager.update(db, incoming)
        return {
            "success": True,
            "message": "Yedek geri yüklendi.",
            "details": results,
            "settings": SettingsManager.current().to_dict(),
        }
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        _logger.error("import_settings_backup_api hatası", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Yedek geri yüklenemedi: {e}")


async def invalidate_admin_sessions_api():
    """Tüm aktif yönetici oturumlarını geçersiz kılar (herkes yeniden /start ile giriş yapmalı)."""
    try:
        await db.invalidate_admin_session()
        return {"success": True, "message": "Tüm yönetici oturumları sonlandırıldı."}
    except Exception as e:
        _logger.error("invalidate_admin_sessions_api hatası", exc_info=True)
        raise HTTPException(status_code=500, detail="Oturumlar sonlandırılamadı")


# --- API Routes: Ayarlar (Settings) — Dosya Ekle (rclone.conf / gdrive_token.pickle) ---
# NOT: /ayarlar Telegram komutundaki dosya yükleme mantığıyla aynı dosya yollarını
# ve MongoDB kalıcılık şemasını (bot_files koleksiyonu) kullanır; bkz.
# Backend/pyrofork/plugins/ayarlar.py ve Backend/__main__.py:_restore_persistent_files

_PROJECT_ROOT       = pathlib.Path(__file__).resolve().parent.parent.parent.parent
_GDRIVE_TOKEN_PATH  = _PROJECT_ROOT / "gdrive_token.pickle"
_RCLONE_CONF_PATH   = _PROJECT_ROOT / "rclone.conf"

_SETTINGS_FILE_TYPES = {
    "gdrive_pickle": {"path": _GDRIVE_TOKEN_PATH, "label": "gdrive_token.pickle", "mongo_id": "gdrive_pickle"},
    "rclone_conf":   {"path": _RCLONE_CONF_PATH,  "label": "rclone.conf",         "mongo_id": "rclone_conf"},
}


def _rclone_remotes_list() -> list:
    if not _RCLONE_CONF_PATH.exists():
        return []
    try:
        import configparser
        rcp = configparser.ConfigParser()
        rcp.read(str(_RCLONE_CONF_PATH))
        return rcp.sections()
    except Exception:
        return []


async def get_settings_files_api():
    """Panelde 'Dosya Ekle' bölümü için mevcut dosya durumlarını döner."""
    return {
        "success": True,
        "files": {
            "gdrive_pickle": {
                "exists": _GDRIVE_TOKEN_PATH.exists(),
                "file_name": "gdrive_token.pickle",
            },
            "rclone_conf": {
                "exists": _RCLONE_CONF_PATH.exists(),
                "file_name": "rclone.conf",
                "remotes": _rclone_remotes_list(),
            },
        },
    }


async def upload_settings_file_api(file_type: str, file: UploadFile):
    """token.pickle / rclone.conf dosyasını panelden yükler; diske ve
    MongoDB'ye (bot_files) kaydeder — Telegram komutundaki davranışın aynısı."""
    info = _SETTINGS_FILE_TYPES.get(file_type)
    if not info:
        raise HTTPException(status_code=400, detail="Geçersiz dosya türü")

    fname = (file.filename or "").lower()

    if file_type == "gdrive_pickle":
        if ".pickle" not in fname:
            raise HTTPException(status_code=400, detail=f"Yalnızca .pickle uzantılı dosya kabul edilir. Gelen dosya adı: {file.filename}")
    elif file_type == "rclone_conf":
        if not (fname == "rclone.conf" or fname.endswith(".conf") or "rclone" in fname):
            raise HTTPException(status_code=400, detail=f"Yalnızca rclone.conf dosyası kabul edilir. Gelen dosya adı: {file.filename}")

    try:
        data = await file.read()
    except Exception as e:
        _logger.error("upload_settings_file_api okuma hatası", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Dosya okunamadı: {e}")

    if not data:
        raise HTTPException(status_code=400, detail="Dosya boş.")

    try:
        info["path"].write_bytes(data)
    except Exception as e:
        _logger.error("upload_settings_file_api yazma hatası", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Dosya kaydedilemedi: {e}")

    try:
        await db.dbs["tracking"]["bot_files"].update_one(
            {"_id": info["mongo_id"]},
            {"$set": {"data": data, "file_name": info["label"]}},
            upsert=True,
        )
    except Exception as _e:
        _logger.warning(f"[settings] {info['label']} MongoDB kaydı başarısız: {_e}")

    return {
        "success": True,
        "message": f"{info['label']} kaydedildi.",
        "remotes": _rclone_remotes_list() if file_type == "rclone_conf" else None,
    }


async def delete_settings_file_api(file_type: str):
    """token.pickle / rclone.conf dosyasını panelden diskten ve MongoDB'den siler."""
    info = _SETTINGS_FILE_TYPES.get(file_type)
    if not info:
        raise HTTPException(status_code=400, detail="Geçersiz dosya türü")

    try:
        if info["path"].exists():
            info["path"].unlink()
    except Exception as e:
        _logger.error("delete_settings_file_api silme hatası", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Dosya silinemedi: {e}")

    try:
        await db.dbs["tracking"]["bot_files"].delete_one({"_id": info["mongo_id"]})
    except Exception as _e:
        _logger.warning(f"[settings] {info['label']} MongoDB kaydı silinemedi: {_e}")

    return {"success": True, "message": f"{info['label']} silindi."}


# ----- ── Sistem & Bakım (Ayarlar sayfası: Veritabanı & Sistem İstatistikleri + Loglar) ──

LOG_FILE = "log.txt"


async def get_db_stats_api() -> dict:
    """Tüm depolama veritabanlarındaki içerik + sistem metriklerini toplar (Ayarlar > Sistem & Bakım)."""
    try:
        from Backend.helper.pyro import get_readable_file_size

        db_stats = await db.get_database_stats()
        total_movies = sum(stat.get("movie_count", 0) for stat in db_stats)
        total_tv = sum(stat.get("tv_count", 0) for stat in db_stats)
        total_db_size = sum(stat.get("dataSize", 0) for stat in db_stats)

        total_episodes = total_streams = 0
        for key in db.dbs.keys():
            if not key.startswith("storage_"):
                continue
            storage = db.dbs[key]

            # Film yayın (stream) sayısı — sunucu tarafında (aggregation) hesaplanır
            movie_pipeline = [
                {"$project": {"n": {"$size": {"$ifNull": ["$telegram", []]}}}},
                {"$group": {"_id": None, "total": {"$sum": "$n"}}},
            ]
            async for row in storage["movie"].aggregate(movie_pipeline):
                total_streams += row.get("total", 0)

            # Dizi bölüm + yayın sayısı — sunucu tarafında (aggregation) hesaplanır
            tv_pipeline = [
                {"$unwind": {"path": "$seasons", "preserveNullAndEmptyArrays": False}},
                {"$unwind": {"path": "$seasons.episodes", "preserveNullAndEmptyArrays": False}},
                {"$group": {
                    "_id": None,
                    "episodes": {"$sum": 1},
                    "streams": {"$sum": {"$size": {"$ifNull": ["$seasons.episodes.telegram", []]}}},
                }},
            ]
            async for row in storage["tv"].aggregate(tv_pipeline):
                total_episodes += row.get("episodes", 0)
                total_streams += row.get("streams", 0)

        from Backend.helper.settings_manager import SettingsManager
        auth_channels = len(SettingsManager.current().auth_channels)

        return {
            "status": "success",
            "data": {
                "version": __version__,
                "movies": total_movies,
                "tv_shows": total_tv,
                "episodes": total_episodes,
                "streams": total_streams,
                "uptime": get_readable_time(time() - StartTime),
                "db_size": get_readable_file_size(total_db_size),
                "storage_dbs": db.current_db_index,
                "auth_channels": auth_channels,
            },
        }
    except Exception as e:
        _logger.error("get_db_stats_api hatası", exc_info=True)
        return {"status": "error", "message": str(e)}


async def get_logs_api(lines: int = 300) -> dict:
    """Web log görüntüleyicisi için log dosyasının son satırlarını döner."""
    import os
    path = os.path.abspath(LOG_FILE)
    if not os.path.exists(path):
        return {"status": "error", "message": "Log dosyası bulunamadı.", "log": ""}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            tail = f.readlines()[-max(1, min(lines, 2000)):]
        return {"status": "success", "log": "".join(tail)}
    except Exception as e:
        return {"status": "error", "message": str(e), "log": ""}


async def download_logs_api():
    """Ham log dosyasını indirir."""
    import os
    from fastapi.responses import FileResponse
    path = os.path.abspath(LOG_FILE)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Log dosyası bulunamadı.")
    return FileResponse(path, filename="log.txt", media_type="text/plain")


async def restart_bot_api() -> dict:
    """
    Botu ve web panelini yeniden başlatır — /restart komutuyla aynı mantık
    (uv run update.py sonrası os.execl ile süreç yeniden başlatılır).
    Ayarlar sayfasındaki "Botu Yeniden Başlat" butonundan tetiklenir.
    """
    import shutil
    from asyncio import create_subprocess_exec
    from os import execl as osexecl

    async def _do_restart():
        # Yanıtın tarayıcıya ulaşması için kısa bir bekleme
        await asyncio.sleep(0.8)
        try:
            proc = await create_subprocess_exec('uv', 'run', 'update.py')
            await proc.wait()
        except Exception as e:
            _logger.warning(f"[restart] update.py çalıştırılamadı: {e}")

        _logger.info("[restart] Ayarlar panelinden yeniden başlatma tetiklendi.")
        uv_path = shutil.which("uv")
        if uv_path:
            osexecl(uv_path, uv_path, "run", "-m", "Backend")
        else:
            _logger.error("[restart] 'uv' PATH içinde bulunamadı, yeniden başlatma iptal edildi.")

    asyncio.create_task(_do_restart())
    return {"success": True, "message": "Bot yeniden başlatılıyor…"}
