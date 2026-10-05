"""
Schema Linker — Embedding-based Table Retrieval.

Given a user's natural-language question, retrieves the most relevant
database tables by computing cosine similarity between the question
embedding and pre-computed table description embeddings.

This is the critical bridge between the user's intent and the SQL
generator's prompt: instead of dumping ALL table DDLs into the prompt
(which bloats tokens and confuses the model on 100+ table databases),
we retrieve only the top-k relevant tables.

For Olist (~9 tables), this provides modest benefit.
For 100+ table enterprise DBs, this is essential for SQL quality.

Embedding model: all-MiniLM-L6-v2 (22M params, runs on CPU, <50ms)

Usage:
    from src.schema.linker import SchemaLinker

    linker = SchemaLinker(settings, schema_inspector)
    linked = linker.link("What is total revenue by product category?")
    # Returns: ["order_items", "products", "product_category"]
"""

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
    """
    Retrieves relevant database tables for a given natural-language query.

    Uses sentence-transformers to embed both table descriptions and
    user queries, then returns top-k tables by cosine similarity.
    """

    def __init__(
        self,
        settings: Settings,
        table_texts: dict[str, str] | None = None,
    ) -> None:
        """
        Initialize the schema linker.

        Args:
            settings: Application settings.
            table_texts: Pre-computed table embedding texts from
                         SchemaInspector.get_embedding_texts().
                         If None, must call build_index() before linking.
        """
        self._settings = settings
        self._model = None  # Lazy-loaded
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
        """Lazy-load the sentence-transformers model."""
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            logger.info(
                "Loading embedding model: %s", self._settings.embedding_model
            )
            self._model = SentenceTransformer(self._settings.embedding_model)
            logger.info("Embedding model loaded")
        return self._model

    def build_index(self, table_texts: dict[str, str]) -> None:
        """
        Build the embedding index from table descriptions.

        Args:
            table_texts: Dict of table_name → embedding text
                         (from SchemaInspector.get_embedding_texts())
        """
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

        # Cache for future startup speed
        self._save_cache()

    def link(
        self,
        query: str,
        top_k: int | None = None,
        threshold: float = 0.15,
    ) -> list[str]:
        """
        Retrieve the most relevant table names for a query.

        Args:
            query: Natural-language user question.
            top_k: Number of tables to return. Defaults to settings.
            threshold: Minimum similarity score to include a table.

        Returns:
            List of table names sorted by relevance (most relevant first).
        """
        if self._table_embeddings is None:
            if not self._load_cache():
                raise RuntimeError(
                    "Schema linker index not built. "
                    "Call build_index() or provide table_texts in constructor."
                )

        model = self._load_model()
        k = top_k or self._settings.schema_linker_top_k

        # Embed the query
        query_embedding = model.encode(
            [query],
            normalize_embeddings=True,
            show_progress_bar=False,
        )

        # Cosine similarity (embeddings are pre-normalized)
        similarities = np.dot(self._table_embeddings, query_embedding.T).flatten()

        # Get top-k indices
        top_indices = np.argsort(similarities)[::-1][:k]

        # Filter by threshold
        results = []
        for idx in top_indices:
            score = float(similarities[idx])
            if score >= threshold:
                results.append(self._table_names[idx])
                logger.debug(
                    "  Linked: %s (score=%.3f)", self._table_names[idx], score
                )

        # If nothing passes threshold, return top result anyway
        if not results and len(self._table_names) > 0:
            best_idx = int(top_indices[0])
            results.append(self._table_names[best_idx])
            logger.info(
                "No table above threshold %.2f; returning best match: %s (%.3f)",
                threshold,
                self._table_names[best_idx],
                float(similarities[best_idx]),
            )

        logger.info("Schema link: '%s' → %s", query[:80], results)
        return results

    def link_with_scores(
        self,
        query: str,
        top_k: int | None = None,
    ) -> list[tuple[str, float]]:
        """
        Link with similarity scores for debugging/display.

        Returns:
            List of (table_name, similarity_score) tuples.
        """
        if self._table_embeddings is None:
            if not self._load_cache():
                raise RuntimeError("Schema linker index not built.")

        model = self._load_model()
        k = top_k or self._settings.schema_linker_top_k

        query_embedding = model.encode(
            [query],
            normalize_embeddings=True,
            show_progress_bar=False,
        )

        similarities = np.dot(self._table_embeddings, query_embedding.T).flatten()
        top_indices = np.argsort(similarities)[::-1][:k]

        return [
            (self._table_names[int(idx)], float(similarities[int(idx)]))
            for idx in top_indices
        ]

    # ── Cache management ─────────────────────────────────────────────

    def _save_cache(self) -> None:
        """Persist embeddings to disk for faster startup."""
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
        """Load cached embeddings if available."""
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
