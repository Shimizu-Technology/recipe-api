import importlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.config import get_settings
from app.public_identity import public_contributor_id, visible_recipe_user_id

if TYPE_CHECKING:
    from app.models.recipe import Recipe


def _load_recipe_routers(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@example.com/db")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    get_settings.cache_clear()

    import app.routers.extract as extract
    import app.routers.recipes as recipes

    return importlib.reload(extract), importlib.reload(recipes)


def _public_recipe() -> "Recipe":
    # Import only after _load_recipe_routers has installed isolated test settings.
    from app.models.recipe import Recipe

    return Recipe(
        id=uuid4(),
        source_url="https://example.com/recipe",
        source_type="website",
        raw_text="private transcript and extraction context",
        extracted={
            "title": "Red Rice",
            "sourceUrl": "https://example.com/recipe",
            "components": [],
            "nutrition": {"perServing": {}, "total": {}},
        },
        created_at=datetime.now(UTC),
        user_id="user_private_clerk_subject",
        extractor_display_name="Test Cook",
        is_public=True,
        has_audio_transcript=False,
    )


def test_public_contributor_id_is_stable_and_opaque():
    first = public_contributor_id("user_private_clerk_subject")
    second = public_contributor_id("user_private_clerk_subject")

    assert first == second
    assert first.startswith("chef_")
    assert "user_private_clerk_subject" not in first
    assert public_contributor_id("user_other_subject") != first


def test_visible_recipe_user_id_preserves_only_the_viewers_own_subject():
    owner_id = "user_private_clerk_subject"

    assert visible_recipe_user_id(owner_id, owner_id) == owner_id
    assert visible_recipe_user_id(owner_id, None) == public_contributor_id(owner_id)
    assert visible_recipe_user_id(owner_id, "user_someone_else") == public_contributor_id(owner_id)


def test_public_detail_redacts_source_text_and_internal_subject(monkeypatch):
    _, recipes = _load_recipe_routers(monkeypatch)
    recipe = _public_recipe()

    response = recipes.recipe_to_detail_response(recipe, viewer_user_id=None)

    assert response.raw_text is None
    assert response.user_id == public_contributor_id(recipe.user_id)
    assert response.contributor_id == public_contributor_id(recipe.user_id)
    assert response.is_owner is False


def test_owner_detail_keeps_owner_debug_fields(monkeypatch):
    _, recipes = _load_recipe_routers(monkeypatch)
    recipe = _public_recipe()

    response = recipes.recipe_to_detail_response(recipe, viewer_user_id=recipe.user_id)

    assert response.raw_text == recipe.raw_text
    assert response.user_id == recipe.user_id
    assert response.contributor_id == public_contributor_id(recipe.user_id)
    assert response.is_owner is True


def test_recipe_creation_defaults_are_private(monkeypatch):
    extract, recipes = _load_recipe_routers(monkeypatch)

    assert extract.ExtractRequest.model_fields["is_public"].default is False
    assert recipes.ManualRecipeCreate.model_fields["is_public"].default is False
    assert recipes.OCRRecipeCreate.model_fields["is_public"].default is False


def test_idempotency_keys_are_payload_scoped(monkeypatch):
    extract, _ = _load_recipe_routers(monkeypatch)
    from app.models.recipe import ExtractionJob

    job = ExtractionJob(
        id=uuid4(),
        url="https://example.com/recipe",
        user_id="user_test",
        location="Guam",
        notes="family version",
        status="queued",
        job_kind="extract",
        requested_is_public=False,
    )

    extract._validate_idempotent_job(
        job,
        job_kind="extract",
        url=job.url,
        location="Guam",
        notes="family version",
        is_public=False,
    )

    with pytest.raises(HTTPException) as conflict:
        extract._validate_idempotent_job(
            job,
            job_kind="extract",
            url="https://example.com/different",
            location="Guam",
            notes="family version",
            is_public=False,
        )
    assert conflict.value.status_code == 409


def test_blank_idempotency_key_is_rejected(monkeypatch):
    extract, _ = _load_recipe_routers(monkeypatch)

    with pytest.raises(HTTPException) as invalid:
        extract._normalized_idempotency_key("   ")
    assert invalid.value.status_code == 400


def test_active_job_deduplication_rejects_changed_options(monkeypatch):
    extract, _ = _load_recipe_routers(monkeypatch)
    from app.models.recipe import ExtractionJob

    job = ExtractionJob(
        id=uuid4(),
        url="https://example.com/recipe",
        user_id="user_test",
        location="Guam",
        notes="original",
        status="processing",
        job_kind="extract",
        requested_is_public=False,
    )

    with pytest.raises(HTTPException) as conflict:
        extract._require_matching_active_job(
            job,
            job_kind="extract",
            url=job.url,
            location="Guam",
            notes="changed",
            is_public=True,
        )
    assert conflict.value.status_code == 409
    assert "different options" in conflict.value.detail


def test_active_extraction_matching_includes_recipe_attribution(monkeypatch):
    extract, _ = _load_recipe_routers(monkeypatch)
    from app.models.recipe import ExtractionJob

    job = ExtractionJob(
        id=uuid4(),
        url="https://example.com/recipe",
        user_id="user_test",
        location="Guam",
        notes="",
        status="processing",
        job_kind="extract",
        requested_is_public=False,
        requested_display_name="Old Name",
    )

    with pytest.raises(HTTPException) as conflict:
        extract._require_matching_active_job(
            job,
            job_kind="extract",
            url=job.url,
            location="Guam",
            notes="",
            is_public=False,
            display_name="New Name",
        )
    assert conflict.value.status_code == 409
