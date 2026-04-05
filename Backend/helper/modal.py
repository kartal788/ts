from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel, Field

# ---------------------------
# Quality Detail Schema
# ---------------------------
class QualityDetail(BaseModel):
    quality: str
    id: str
    name: str
    size: str
    is_archive: bool = False  # True → ZIP/7Z arşiv; Stremio'da gösterilmez, sadece member catalog'da


# ---------------------------
# Episode Schema
# ---------------------------
class Episode(BaseModel):
    episode_number: int
    title: str
    title_tr: Optional[str] = None
    title_de: Optional[str] = None
    episode_backdrop: Optional[str] = None
    overview: Optional[str] = None
    overview_tr: Optional[str] = None
    overview_de: Optional[str] = None
    released: Optional[str] = None
    telegram: Optional[List[QualityDetail]]


# ---------------------------
# Season Schema
# ---------------------------
class Season(BaseModel):
    season_number: int
    episodes: List[Episode] = Field(default_factory=list)


# ---------------------------
# TV Show Schema
# ---------------------------
class TVShowSchema(BaseModel):
    tmdb_id: Optional[int] = None
    imdb_id: Optional[str] = None
    db_index: int
    title: str
    title_tr: Optional[str] = None
    title_de: Optional[str] = None
    genres: Optional[List[str]] = None
    genres_tr: Optional[List[str]] = None
    genres_de: Optional[List[str]] = None
    description: Optional[str] = None
    description_tr: Optional[str] = None
    description_de: Optional[str] = None
    rating: Optional[float] = None
    release_year: Optional[int] = None
    poster: Optional[str] = None
    backdrop: Optional[str] = None
    logo: Optional[str] = None
    poster_tr: Optional[str] = None
    backdrop_tr: Optional[str] = None
    logo_tr: Optional[str] = None
    poster_de: Optional[str] = None
    backdrop_de: Optional[str] = None
    logo_de: Optional[str] = None
    cast: Optional[List[str]] = None
    runtime: Optional[str] = None
    original_language: Optional[str] = None  # TMDB original_language (ISO 639-1, örn: "en", "tr")
    media_type: str
    certification_tr: Optional[str] = None  # Türkiye sertifikası
    certification_de: Optional[str] = None  # Almanya sertifikası (FSK)
    certification_us: Optional[str] = None  # ABD sertifikası
    updated_on: datetime = Field(default_factory=datetime.utcnow)
    seasons: List[Season] = Field(default_factory=list)


# ---------------------------
# Movie Schema
# ---------------------------
class MovieSchema(BaseModel):
    tmdb_id: Optional[int] = None
    imdb_id: Optional[str] = None
    db_index: int
    title: str
    title_tr: Optional[str] = None
    title_de: Optional[str] = None
    genres: Optional[List[str]] = None
    genres_tr: Optional[List[str]] = None
    genres_de: Optional[List[str]] = None
    description: Optional[str] = None
    description_tr: Optional[str] = None
    description_de: Optional[str] = None
    rating: Optional[float] = None
    release_year: Optional[int] = None
    poster: Optional[str] = None
    backdrop: Optional[str] = None
    logo: Optional[str] = None
    poster_tr: Optional[str] = None
    backdrop_tr: Optional[str] = None
    logo_tr: Optional[str] = None
    poster_de: Optional[str] = None
    backdrop_de: Optional[str] = None
    logo_de: Optional[str] = None
    cast: Optional[List[str]] = None
    runtime: Optional[str] = None
    original_language: Optional[str] = None  # TMDB original_language (ISO 639-1, örn: "en", "tr")
    media_type: str
    collection_id: Optional[int] = None   # TMDB belongs_to_collection.id
    certification_tr: Optional[str] = None  # Türkiye sertifikası
    certification_de: Optional[str] = None  # Almanya sertifikası (FSK)
    certification_us: Optional[str] = None  # ABD sertifikası (MPAA)
    updated_on: datetime = Field(default_factory=datetime.utcnow)
    telegram: Optional[List[QualityDetail]]
