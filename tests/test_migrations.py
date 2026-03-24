from pathlib import Path

from alembic.config import Config
from sqlalchemy import create_engine, inspect

from alembic import command


def test_alembic_upgrade_creates_phase1_tables(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "alembic_phase1.db"
    database_url = f"sqlite:///{db_path}"

    monkeypatch.delenv("ALEMBIC_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    alembic_cfg = Config("alembic.ini")
    alembic_cfg.set_main_option("sqlalchemy.url", database_url)

    command.upgrade(alembic_cfg, "head")

    engine = create_engine(database_url)
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    expected_tables = {
        "business_profile",
        "operator",
        "conversation_session",
        "ad_draft",
        "draft_product",
        "ad_variant_round",
        "ad_variant",
        "published_ad",
        "system_config",
        "audit_event",
        "processed_inbound_message",
    }
    assert expected_tables.issubset(tables)

    ad_draft_columns = {column["name"] for column in inspector.get_columns("ad_draft")}
    assert "enrichment_source" in ad_draft_columns
    assert "request_type" in ad_draft_columns
    assert "generation_ready" in ad_draft_columns
    assert "marketing_brief" in ad_draft_columns

    conversation_columns = {
        column["name"] for column in inspector.get_columns("conversation_session")
    }
    assert "pending_question_type" in conversation_columns
    assert "pending_question_context" in conversation_columns
    assert "product_brand" in ad_draft_columns

    operator_columns = {column["name"] for column in inspector.get_columns("operator")}
    assert {
        "business_name",
        "logo_url",
        "brand_colors",
        "cms_campaign_id",
        "cms_playlist_id",
        "store_type",
        "creative_guidance",
    }.issubset(operator_columns)

    conversation_columns = {
        column["name"] for column in inspector.get_columns("conversation_session")
    }
    assert "pending_upload_type" in conversation_columns
    assert "pending_followup_question" in conversation_columns


def test_alembic_downgrade_removes_product_brand_and_pending_upload(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "alembic_phase1_downgrade.db"
    database_url = f"sqlite:///{db_path}"

    monkeypatch.delenv("ALEMBIC_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    alembic_cfg = Config("alembic.ini")
    alembic_cfg.set_main_option("sqlalchemy.url", database_url)

    command.upgrade(alembic_cfg, "head")
    command.downgrade(alembic_cfg, "20260304_0003")

    engine = create_engine(database_url)
    inspector = inspect(engine)

    ad_draft_columns = {column["name"] for column in inspector.get_columns("ad_draft")}
    assert "product_brand" not in ad_draft_columns

    conversation_columns = {
        column["name"] for column in inspector.get_columns("conversation_session")
    }
    assert "pending_upload_type" not in conversation_columns


def test_alembic_downgrade_removes_operator_cms_mapping_columns(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "alembic_phase1_operator_mapping_downgrade.db"
    database_url = f"sqlite:///{db_path}"

    monkeypatch.delenv("ALEMBIC_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    alembic_cfg = Config("alembic.ini")
    alembic_cfg.set_main_option("sqlalchemy.url", database_url)

    command.upgrade(alembic_cfg, "head")
    command.downgrade(alembic_cfg, "20260304_0004")

    engine = create_engine(database_url)
    inspector = inspect(engine)

    operator_columns = {column["name"] for column in inspector.get_columns("operator")}
    assert "cms_campaign_id" not in operator_columns
    assert "cms_playlist_id" not in operator_columns


def test_alembic_downgrade_removes_operator_system_memory_columns(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "alembic_operator_system_memory_downgrade.db"
    database_url = f"sqlite:///{db_path}"

    monkeypatch.delenv("ALEMBIC_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    alembic_cfg = Config("alembic.ini")
    alembic_cfg.set_main_option("sqlalchemy.url", database_url)

    command.upgrade(alembic_cfg, "head")
    command.downgrade(alembic_cfg, "20260305_0005")

    engine = create_engine(database_url)
    inspector = inspect(engine)
    operator_columns = {column["name"] for column in inspector.get_columns("operator")}
    assert "store_type" not in operator_columns
    assert "creative_guidance" not in operator_columns


def test_alembic_downgrade_removes_pending_followup_question(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "alembic_followup_question_downgrade.db"
    database_url = f"sqlite:///{db_path}"

    monkeypatch.delenv("ALEMBIC_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    alembic_cfg = Config("alembic.ini")
    alembic_cfg.set_main_option("sqlalchemy.url", database_url)

    command.upgrade(alembic_cfg, "head")
    command.downgrade(alembic_cfg, "20260305_0006")

    engine = create_engine(database_url)
    inspector = inspect(engine)
    conversation_columns = {
        column["name"] for column in inspector.get_columns("conversation_session")
    }
    assert "pending_followup_question" not in conversation_columns


def test_alembic_downgrade_removes_marketing_brief(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "alembic_marketing_brief_downgrade.db"
    database_url = f"sqlite:///{db_path}"

    monkeypatch.delenv("ALEMBIC_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    alembic_cfg = Config("alembic.ini")
    alembic_cfg.set_main_option("sqlalchemy.url", database_url)

    command.upgrade(alembic_cfg, "head")
    command.downgrade(alembic_cfg, "20260316_0009")

    engine = create_engine(database_url)
    inspector = inspect(engine)
    ad_draft_columns = {column["name"] for column in inspector.get_columns("ad_draft")}
    assert "marketing_brief" not in ad_draft_columns
