from __future__ import annotations

import logging
import pickle
from pathlib import Path
from typing import Any, Optional

import numpy as np
from numpy.typing import NDArray

from src.config import Settings

logger = logging.getLogger(__name__)


class SchemaLinker:
    def __init__(
        self,
        settings: Settings,
        table_texts: dict[str, str] | None = None,
    ) -> None:
        self._settings = settings
        self._model = None
        self._table_names: list[str] = []
        self._table_embeddings: NDArray | None = None
        self._cache_dir = (
            Path(settings.database_path).parent.parent
            / "src" / "schema" / "embeddings_cache"
        )
        self._cache_dir.mkdir(parents=True, exist_ok=True)

        if table_texts:
            self.build_index(table_texts)

    def _load_model(self) -> Any:
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            logger.info(
                "Loading embedding model: %s", self._settings.embedding_model
            )
            self._model = SentenceTransformer(self._settings.embedding_model)
            logger.info("Embedding model loaded")
        return self._model

    def build_index(self, table_texts: dict[str, str]) -> None:
        model = self._load_model()

        self._table_names = list(table_texts.keys())
        texts = list(table_texts.values())

        logger.info("Embedding %d table descriptions...", len(texts))
        self._table_embeddings = model.encode(
            texts,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        logger.info("Table embeddings built: shape %s", self._table_embeddings.shape)

        self._save_cache()

    def link(
        self,
        query: str,
        top_k: int | None = None,
        threshold: float = 0.15,
    ) -> list[str]:
        if self._table_embeddings is None:
            if not self._load_cache():
                raise RuntimeError(
                    "Schema linker index not built. "
                    "Call build_index() or provide table_texts in constructor."
                )

        model = self._load_model()
        k = top_k or self._settings.schema_linker_top_k

        query_embedding = model.encode(
            [query],
            normalize_embeddings=True,
            show_progress_bar=False,
        )

        similarities = np.dot(self._table_embeddings, query_embedding.T).flatten()
        top_indices = np.argsort(similarities)[::-1][:k]

        results = []
        for idx in top_indices:
            score = float(similarities[idx])
            if score >= threshold:
                results.append(self._table_names[idx])
                logger.debug(
                    "  Linked: %s (score=%.3f)", self._table_names[idx], score
                )

        if not results and len(self._table_names) > 0:
            best_idx = int(top_indices[0])
            results.append(self._table_names[best_idx])
            logger.info(
                "No table above threshold %.2f; returning best match: %s (%.3f)",
                threshold,
                self._table_names[best_idx],
                float(similarities[best_idx]),
            )

        logger.info(
            "Schema linker for '%s' → %s (top-%d, threshold=%.2f)",
            query[:60],
            results,
            k,
            threshold,
        )

        return results

    def get_similarities(
        self, query: str, top_k: int = 5
    ) -> list[tuple[str, float]]:
        if self._table_embeddings is None:
            if not self._load_cache():
                return []

        model = self._load_model()
        query_embedding = model.encode(
            [query],
            normalize_embeddings=True,
            show_progress_bar=False,
        )

        k = min(top_k, len(self._table_names))
        similarities = np.dot(self._table_embeddings, query_embedding.T).flatten()
        top_indices = np.argsort(similarities)[::-1][:k]

        return [
            (self._table_names[int(idx)], float(similarities[int(idx)]))
            for idx in top_indices
        ]

    def _save_cache(self) -> None:
        try:
            cache_data = {
                "table_names": self._table_names,
                "embeddings": self._table_embeddings,
            }
            cache_path = self._cache_dir / "table_embeddings.pkl"
            with open(cache_path, "wb") as f:
                pickle.dump(cache_data, f)
            logger.info("Embeddings cached to %s", cache_path)
        except Exception:
            logger.warning("Failed to cache embeddings", exc_info=True)

    def _load_cache(self) -> bool:
        cache_path = self._cache_dir / "table_embeddings.pkl"
        if not cache_path.exists():
            return False

        try:
            with open(cache_path, "rb") as f:
                cache_data = pickle.load(f)
            self._table_names = cache_data["table_names"]
            self._table_embeddings = cache_data["embeddings"]
            logger.info(
                "Loaded cached embeddings: %d tables", len(self._table_names)
            )
            return True
        except Exception:
            logger.warning("Failed to load cached embeddings", exc_info=True)
            return False
